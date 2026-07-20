"""Local SQLite + FTS5 index for the scientist workbench."""

import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Callable, Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

from ..redaction.secrets import redact_text
from ..scoring.badges import compute_all_badges
from ..config import (
    CONFIG_DIR,
    load_config,
    mark_auto_upload_profile_changed,
    normalize_excluded_project_names,
    source_scope_sources,
)
from ..paths import ensure_install_files
from ..pricing import estimate_cost

INDEX_DB = CONFIG_DIR / "index.db"
BLOBS_DIR = CONFIG_DIR / "blobs"

# Schema version sentinels. Version 1 is the bundles→shares migration,
# version 2 is the security refactor, version 3 adds the
# `sessions.session_key` bridge to `event_sessions.session_key`, and
# version 4 marks the workbench as widened-message-aware (parser path
# now produces messages with optional `invocations` / `snippets` /
# `extra` / `author` fields, all routed through redaction), version 5
# stores hosted-submission receipts, version 6 tracks immutable
# content revisions for re-sharing traces that continue to grow, and
# version 7 adds the local automatic-upload enrollment/candidate foundation,
# and version 8 adds durable per-agent hook observations plus the raw-source
# fingerprint snapshot needed to recover a sealed artifact safely.
SECURITY_SCHEMA_VERSION = 2
SESSION_IDENTITY_SCHEMA_VERSION = 3
WIDENED_MESSAGE_SCHEMA_VERSION = 4
HOSTED_SUBMISSION_SCHEMA_VERSION = 5
REVISION_TRACKING_SCHEMA_VERSION = 6
AUTO_UPLOAD_FOUNDATION_SCHEMA_VERSION = 7
AUTO_UPLOAD_SCHEMA_VERSION = 8
WORKBENCH_SCHEMA_VERSION = AUTO_UPLOAD_SCHEMA_VERSION
BACKFILL_WINDOW = 100
FAILURE_VALUE_SOURCE_SCOPE = ("claude", "claude-science", "codex", "opencode", "openclaw", "workbuddy")
SHARE_RECOMMENDATION_LIMIT = 10
AUTO_UPLOAD_CANDIDATE_LIMIT = 5
AUTO_UPLOAD_STABILITY_HOURS = 24


class RevisionConflictError(ValueError):
    """A selected trace no longer matches the revision a caller reviewed."""

    def __init__(self, blockers: list[dict[str, Any]]):
        self.blockers = blockers
        session_ids = ", ".join(
            str(blocker.get("session_id", "unknown")) for blocker in blockers
        )
        super().__init__(f"Trace revisions changed before share creation: {session_ids}")


# Display-only normalization from the mixed AI/heuristic outcome vocabulary
# onto a single coherent label set. Prevents the duplicate-meaning rows we
# used to get (AI `resolved` next to heuristic `tests_passed`; AI `partial`
# colliding with heuristic `partial` that means "user spoke last"). AI
# labels take precedence when present; invalid judge output maps to
# `unknown` (validation at the write path now prevents this from growing).
# Keep columns selectable everywhere by wrapping in `({_OUTCOME_NORMALIZE_SQL}) as outcome_label`.
_OUTCOME_NORMALIZE_SQL = (
    "CASE "
    "WHEN ai_outcome_badge = 'resolved'    THEN 'resolved' "
    "WHEN ai_outcome_badge = 'partial'     THEN 'partial' "
    "WHEN ai_outcome_badge = 'failed'      THEN 'failed' "
    "WHEN ai_outcome_badge = 'abandoned'   THEN 'abandoned' "
    "WHEN ai_outcome_badge = 'exploratory' THEN 'exploratory' "
    "WHEN ai_outcome_badge = 'trivial'     THEN 'trivial' "
    "WHEN ai_outcome_badge IS NOT NULL     THEN 'unknown' "
    "WHEN outcome_badge = 'tests_passed'   THEN 'resolved' "
    "WHEN outcome_badge = 'tests_failed'   THEN 'failed' "
    "WHEN outcome_badge = 'build_failed'   THEN 'failed' "
    "WHEN outcome_badge = 'errored'        THEN 'failed' "
    "WHEN outcome_badge = 'completed'      THEN 'inconclusive' "
    "WHEN outcome_badge = 'partial'        THEN 'interrupted' "
    "WHEN outcome_badge = 'analysis_only'  THEN 'exploratory' "
    "ELSE 'unscored' "
    "END"
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id         TEXT PRIMARY KEY,
    project            TEXT NOT NULL,
    source             TEXT NOT NULL,
    model              TEXT,
    model_effort       TEXT,
    start_time         TEXT,
    end_time           TEXT,
    duration_seconds   INTEGER,
    git_branch         TEXT,
    user_messages      INTEGER DEFAULT 0,
    assistant_messages INTEGER DEFAULT 0,
    tool_uses          INTEGER DEFAULT 0,
    input_tokens       INTEGER DEFAULT 0,
    output_tokens      INTEGER DEFAULT 0,
    cache_read_tokens       INTEGER DEFAULT 0,
    cache_creation_tokens   INTEGER DEFAULT 0,
    display_title      TEXT,
    outcome_badge      TEXT,
    value_badges       TEXT,
    risk_badges        TEXT,
    sensitivity_score  REAL DEFAULT 0.0,
    task_type          TEXT,
    files_touched      TEXT,
    commands_run       TEXT,
    review_status      TEXT DEFAULT 'new',
    selection_reason   TEXT,
    reviewer_notes     TEXT,
    reviewed_at        TEXT,
    blob_path          TEXT,
    raw_source_path    TEXT,
    session_key        TEXT,
    indexed_at         TEXT NOT NULL,
    updated_at         TEXT,
    share_id           TEXT REFERENCES shares(share_id),
    ai_quality_score   INTEGER,
    ai_score_reason    TEXT,
    ai_display_title   TEXT,
    ai_failure_value_score INTEGER,
    ai_recovery_labels     TEXT,
    ai_failure_attribution TEXT,
    ai_failure_modes       TEXT,
    ai_learning_summary    TEXT,
    ai_scorer_backend      TEXT,
    ai_scorer_model        TEXT,
    ai_rubric_git_sha      TEXT,
    ai_scored_at           TEXT,
    content_revision       TEXT,
    revision_stable_since  TEXT
);

CREATE TABLE IF NOT EXISTS shares (
    share_id        TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    session_count   INTEGER,
    status          TEXT DEFAULT 'draft',
    attestation     TEXT,
    submission_note TEXT,
    bundle_hash     TEXT,
    manifest        TEXT,
    shared_at       TEXT,
    gcs_uri         TEXT,
    hosted_receipt_id TEXT,
    hosted_status     TEXT,
    hosted_submission_url TEXT,
    submission_channel    TEXT,
    enrollment_id         TEXT,
    client_submission_id  TEXT,
    authorization_revision INTEGER,
    submission_state      TEXT,
    sealed_artifact_sha256 TEXT,
    sealed_artifact_path   TEXT,
    sealed_raw_fingerprints TEXT
);

CREATE TABLE IF NOT EXISTS share_sessions (
    share_id          TEXT NOT NULL REFERENCES shares(share_id),
    session_id        TEXT NOT NULL REFERENCES sessions(session_id),
    added_at          TEXT NOT NULL,
    content_revision  TEXT,
    replaces_revision TEXT,
    PRIMARY KEY (share_id, session_id)
);

CREATE TABLE IF NOT EXISTS policies (
    policy_id    TEXT PRIMARY KEY,
    policy_type  TEXT NOT NULL,
    value        TEXT NOT NULL,
    reason       TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(review_status);
CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS idx_sessions_start_time ON sessions(start_time);
CREATE INDEX IF NOT EXISTS idx_share_sessions_session_id ON share_sessions(session_id);

CREATE TABLE IF NOT EXISTS auto_upload_enrollment (
    singleton_id                    INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    mode                            TEXT NOT NULL CHECK (mode IN ('off', 'enabled', 'paused')),
    health                          TEXT NOT NULL CHECK (health IN ('ready', 'action_required', 'retrying')),
    generation                      INTEGER NOT NULL CHECK (generation >= 1),
    enrolled_at                     TEXT NOT NULL,
    client_enrollment_id            TEXT NOT NULL,
    enrolled_sources_json           TEXT NOT NULL,
    enrolled_projects_json          TEXT NOT NULL,
    server_enrollment_id            TEXT,
    authorization_revision          INTEGER,
    recurring_authorization_version TEXT,
    retention_version               TEXT,
    egress_profile_hash             TEXT,
    hook_targets_json               TEXT NOT NULL DEFAULT '[]',
    claude_hook_observed_at         TEXT,
    codex_hook_observed_at          TEXT,
    last_completed_at               TEXT,
    next_retry_at                   TEXT,
    consecutive_failures            INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    last_result_code                TEXT,
    last_result_count               INTEGER,
    last_receipt_reference          TEXT,
    current_run_id                  TEXT,
    current_run_stage               TEXT,
    revocation_pending              INTEGER NOT NULL DEFAULT 0 CHECK (revocation_pending IN (0, 1)),
    updated_at                      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    finding_id         TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    engine             TEXT NOT NULL,
    rule               TEXT,
    entity_type        TEXT,
    entity_hash        TEXT NOT NULL,
    entity_length      INTEGER,
    field              TEXT NOT NULL,
    message_index      INTEGER,
    tool_field         TEXT,
    offset             INTEGER NOT NULL,
    length             INTEGER NOT NULL,
    confidence         REAL,
    status             TEXT DEFAULT 'open',
    decided_by         TEXT,
    decision_source_id TEXT,
    decided_at         TEXT,
    decision_reason    TEXT,
    revision           TEXT NOT NULL,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_session ON findings(session_id);
CREATE INDEX IF NOT EXISTS idx_findings_status ON findings(status);
CREATE INDEX IF NOT EXISTS idx_findings_revision ON findings(session_id, revision);
CREATE INDEX IF NOT EXISTS idx_findings_entity_hash ON findings(session_id, entity_hash);

CREATE TABLE IF NOT EXISTS findings_allowlist (
    allowlist_id   TEXT PRIMARY KEY,
    entity_type    TEXT,
    entity_hash    TEXT NOT NULL,
    entity_label   TEXT,
    scope          TEXT NOT NULL DEFAULT 'global',
    reason         TEXT,
    added_by       TEXT NOT NULL,
    added_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_allowlist_hash ON findings_allowlist(entity_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_findings_allowlist_typed
    ON findings_allowlist(entity_type, entity_hash, scope)
    WHERE entity_type IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_findings_allowlist_any_type
    ON findings_allowlist(entity_hash, scope)
    WHERE entity_type IS NULL;

CREATE TABLE IF NOT EXISTS session_hold_history (
    history_id     TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    from_state     TEXT,
    to_state       TEXT NOT NULL,
    embargo_until  TEXT,
    changed_by     TEXT NOT NULL,
    changed_at     TEXT NOT NULL,
    reason         TEXT
);
CREATE INDEX IF NOT EXISTS idx_hold_history_session ON session_hold_history(session_id, changed_at);

-- Personalized weekly benchmark tables. Net-new tables ship in SCHEMA_SQL as
-- idempotent CREATE TABLE IF NOT EXISTS (benchmarks before its FK referents);
-- open_index() runs executescript(SCHEMA_SQL) on every open, so existing DBs
-- gain them on next open. No user_version bump or _migrate_* is needed — those
-- are reserved for ALTER/rename/backfill on existing tables. Add a
-- BENCHMARK_SCHEMA_VERSION sentinel + _migrate_ only if a future column
-- ALTER/backfill is required.
CREATE TABLE IF NOT EXISTS benchmarks (
    benchmark_id        TEXT PRIMARY KEY,
    window_start        TEXT NOT NULL,
    window_end          TEXT NOT NULL,
    generated_at        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'ready',
    stage               TEXT,
    backend             TEXT,
    rubric_git_sha      TEXT,
    n_tasks             INTEGER,
    total_points        INTEGER,
    source_count        INTEGER,
    dropped_for_cost    INTEGER,
    ready_count         INTEGER,
    needs_staging_count INTEGER,
    payload_json        TEXT NOT NULL,
    error               TEXT
);
CREATE INDEX IF NOT EXISTS idx_benchmarks_generated ON benchmarks(generated_at DESC);

CREATE TABLE IF NOT EXISTS benchmark_tasks (
    task_id                   TEXT PRIMARY KEY,
    benchmark_id              TEXT NOT NULL REFERENCES benchmarks(benchmark_id) ON DELETE CASCADE,
    title                     TEXT,
    theme                     TEXT,
    domains_json              TEXT,
    source_agents_json        TEXT,
    difficulty                TEXT,
    points                    INTEGER,
    grading                   TEXT,
    readiness                 TEXT,
    leakage_risk              TEXT,
    privacy_risk              TEXT,
    grounded_session_ids_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_benchmark_tasks_run ON benchmark_tasks(benchmark_id);
CREATE INDEX IF NOT EXISTS idx_benchmark_tasks_readiness ON benchmark_tasks(benchmark_id, readiness);

CREATE TABLE IF NOT EXISTS benchmark_exports (
    export_id              TEXT PRIMARY KEY,
    benchmark_id           TEXT NOT NULL REFERENCES benchmarks(benchmark_id) ON DELETE CASCADE,
    kind                   TEXT NOT NULL,
    path                   TEXT,
    created_at             TEXT NOT NULL,
    redaction_summary_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_benchmark_exports_run ON benchmark_exports(benchmark_id);
"""

FTS_SCHEMA_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    session_id,
    display_title,
    transcript_text,
    files_touched,
    commands_run
);
"""

# We use a regular FTS5 table (not contentless) so it stores its own content.
# This avoids rowid synchronization issues with INSERT OR REPLACE on the
# sessions table.  We join on session_id instead of rowid.
# The transcript_text column holds flattened message content for search.


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _revision_json_value(value: Any) -> Any:
    """Return a JSON-stable representation for content revision hashing."""
    if isinstance(value, dict):
        return {
            str(key): _revision_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_revision_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_revision_json_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":"), default=str,
            ),
        )
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def compute_content_revision(session: dict[str, Any]) -> str:
    """Hash the normalized transcript content of a parsed session.

    Session identity, parser metadata, indexing timestamps, review state, and
    scoring fields intentionally do not participate. This means parser-only
    enrichment can refresh metadata without making a reviewed trace stale,
    while an appended or edited message always creates a new revision.
    """
    payload = {
        "messages": _revision_json_value(session.get("messages", [])),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _legacy_content_revision(session_id: str) -> str:
    """Return a stable opaque baseline when a migrated blob is unreadable."""
    digest = hashlib.sha256(f"legacy-session:{session_id}".encode("utf-8")).hexdigest()
    return f"legacy:{digest}"


_WINDOWS_RESERVED_BLOB_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_HASHED_BLOB_PREFIX = "session-sha256-"


def _blob_storage_path(session_id: str) -> Path:
    """Return a cross-platform-safe canonical path for one session blob."""

    basename = session_id.split(".", 1)[0].upper()
    utf8_length = len(session_id.encode("utf-8"))
    utf16_length = len(session_id.encode("utf-16-le")) // 2
    safe_component = bool(session_id) and not any(
        ord(character) < 32 or character in '<>:"/\\|?*'
        for character in session_id
    )
    if (
        safe_component
        and not session_id.startswith(_HASHED_BLOB_PREFIX)
        and not session_id.endswith((" ", "."))
        and basename not in _WINDOWS_RESERVED_BLOB_NAMES
        and utf8_length <= 200
        and utf16_length <= 200
    ):
        filename = f"{session_id}.json"
    else:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        filename = f"{_HASHED_BLOB_PREFIX}{digest}.json"
    return BLOBS_DIR / filename


def _blob_candidate_paths(session_id: str, blob_path: str | None) -> list[Path]:
    candidates: list[Path] = []
    if blob_path:
        candidates.append(Path(blob_path))
    canonical = _blob_storage_path(session_id)
    if canonical not in candidates:
        candidates.append(canonical)
    if session_id and "/" not in session_id and "\\" not in session_id:
        legacy = BLOBS_DIR / f"{session_id}.json"
        if legacy not in candidates:
            candidates.append(legacy)
    return candidates


def resolve_blob_path(session_id: str, blob_path: str | None = None) -> Path | None:
    """Resolve the stored, canonical, or safe legacy blob path."""

    for candidate in _blob_candidate_paths(session_id, blob_path):
        if candidate.is_file():
            return candidate
    return None


def _blob_present_for_revision(session_id: str, blob_path: str | None) -> bool:
    """Cheap presence check for a session blob — no parse.

    The auto-upload candidate report is polled frequently (status/preview) and
    iterates every eligible session; a full ``json.load`` of each blob here
    would make it O(eligible x blob size) in disk I/O. Existence is sufficient
    for the report's ``missing_blob`` gate — the packaging path re-reads and
    validates the blob before any egress, so a present-but-corrupt blob is
    still caught there rather than crossing the machine boundary.
    """
    return resolve_blob_path(session_id, blob_path) is not None


def _read_blob_for_revision(
    session_id: str,
    blob_path: str | None,
) -> dict[str, Any] | None:
    """Load a migration-time blob, tolerating stale stored blob paths."""
    for candidate in _blob_candidate_paths(session_id, blob_path):
        try:
            with open(candidate, encoding="utf-8") as f:
                blob = json.load(f)
            if isinstance(blob, dict):
                return blob
        except (OSError, json.JSONDecodeError):
            continue
    return None


def open_index() -> sqlite3.Connection:
    """Open (and initialize if needed) the index database.

    Creates the database file, tables, indices, and FTS virtual table
    if they do not already exist. Returns a connection with
    row_factory set to sqlite3.Row for dict-like access.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    BLOBS_DIR.mkdir(parents=True, exist_ok=True)

    # Bootstrap per-install salt and API token before any DB-backed code
    # runs so every hash computed against this DB is salted consistently.
    # Files land next to the DB so test-time monkeypatching of INDEX_DB
    # keeps them isolated to the test directory.
    ensure_install_files(Path(str(INDEX_DB)).parent)

    conn = sqlite3.connect(str(INDEX_DB), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")

    # Migrate pre-rename schema (bundles → shares) before executing SCHEMA_SQL,
    # so CREATE TABLE IF NOT EXISTS doesn't see the old tables and skip.
    _migrate_bundles_to_shares(conn)

    conn.executescript(SCHEMA_SQL)

    # FTS5 creation must be separate -- executescript resets transactions
    # and CREATE VIRTUAL TABLE cannot be inside a multi-statement script
    # on some SQLite builds. We handle the case where FTS5 is unavailable.
    try:
        conn.execute(FTS_SCHEMA_SQL.strip())
        conn.commit()
    except sqlite3.OperationalError:
        # FTS5 extension not available -- full-text search will be disabled
        pass

    # Migrations: add columns that may be missing in older databases.
    for col, col_type in [
        ("ai_quality_score", "INTEGER"),
        ("ai_score_reason", "TEXT"),
        ("ai_episode_quality", "REAL"),   # legacy, kept for old DBs
        ("ai_quality_tier", "TEXT"),       # legacy, kept for old DBs
        ("ai_scoring_detail", "TEXT"),
        ("ai_task_type", "TEXT"),
        ("ai_outcome_badge", "TEXT"),
        ("ai_value_badges", "TEXT"),
        ("ai_risk_badges", "TEXT"),
        ("ai_display_title", "TEXT"),
        ("parent_session_id", "TEXT"),
        ("segment_index", "INTEGER"),
        ("segment_start_message", "INTEGER"),
        ("segment_end_message", "INTEGER"),
        ("segment_reason", "TEXT"),
        ("client_origin", "TEXT"),
        ("runtime_channel", "TEXT"),
        ("outer_session_id", "TEXT"),
        ("estimated_cost_usd", "REAL"),
        ("subagent_session_ids", "TEXT"),
        ("ai_effort_estimate", "REAL"),   # replaces ai_episode_quality
        ("ai_summary", "TEXT"),           # replaces ai_quality_tier
        ("ai_failure_value_score", "INTEGER"),
        ("ai_recovery_labels", "TEXT"),
        ("ai_failure_attribution", "TEXT"),
        ("ai_failure_modes", "TEXT"),
        ("ai_learning_summary", "TEXT"),
        ("ai_scorer_backend", "TEXT"),
        ("ai_scorer_model", "TEXT"),
        ("ai_rubric_git_sha", "TEXT"),
        ("ai_scored_at", "TEXT"),
        ("tool_counts", "TEXT"),
        ("user_interrupts", "INTEGER"),
        ("model_effort", "TEXT"),   # Codex-style reasoning effort ("medium"/"high"/"xhigh")
        # Cached-input token buckets. Previously tracked only in the in-memory
        # stats dict (and used for cost estimation), not persisted. Without
        # them the Token Usage chart under-counts Claude input by ~50x.
        ("cache_read_tokens", "INTEGER DEFAULT 0"),
        ("cache_creation_tokens", "INTEGER DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {col_type}")
            conn.commit()
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise
            # Column already exists — ignore.

    for col, col_type in [
        ("shared_at", "TEXT"),
        ("gcs_uri", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE shares ADD COLUMN {col} {col_type}")
            conn.commit()
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise

    _migrate_security_refactor(conn)
    _migrate_session_identity_bridge(conn)
    _migrate_widened_message_model(conn)
    _migrate_hosted_submission_receipts(conn)
    _migrate_revision_tracking(conn)
    _migrate_auto_upload_foundation(conn)
    _migrate_auto_upload_recovery_metadata(conn)

    # Clean up ai_outcome_badge values that the judge wrote before the
    # resolution validator rejected invalid labels. Idempotent: after
    # the first cleanup this UPDATE matches zero rows. Keeps the
    # normalized outcome chart free of silent "unknown" buckets.
    conn.execute(
        "UPDATE sessions SET ai_outcome_badge = NULL "
        "WHERE ai_outcome_badge IS NOT NULL "
        "AND ai_outcome_badge NOT IN "
        "('resolved', 'partial', 'failed', 'abandoned', 'exploratory', 'trivial')"
    )
    conn.commit()

    return conn


def _migrate_security_refactor(conn: sqlite3.Connection) -> None:
    """Add findings/hold-state columns + bounded backfill flagging.

    Advances PRAGMA user_version 1 → 2. Runs once: adds
    `hold_state`, `embargo_until`, `findings_revision`,
    `findings_backfill_needed` to `sessions`; backfills
    `hold_state` from `review_status`; inserts an origin
    `session_hold_history` row per existing session; flags the
    `BACKFILL_WINDOW` most-recently-active sessions for the
    Scanner to pick up. Everything runs inside one transaction —
    partial migration rolls back, so re-running reruns the full
    step cleanly (see Decision 13).
    """
    version_row = conn.execute("PRAGMA user_version").fetchone()
    version = version_row[0] if version_row else 0
    if version >= SECURITY_SCHEMA_VERSION:
        return

    now = _now_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for col, col_type in [
            ("hold_state", "TEXT DEFAULT 'auto_redacted'"),
            ("embargo_until", "TEXT"),
            ("findings_revision", "TEXT"),
            ("findings_backfill_needed", "INTEGER"),
        ]:
            try:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise

        # Backfill hold_state from review_status for rows that existed
        # before the column was added. The column default handles
        # future inserts; here we map the one semantic transition we
        # can recover (approved → released).
        conn.execute(
            "UPDATE sessions SET hold_state = 'released' "
            "WHERE review_status = 'approved' AND (hold_state IS NULL OR hold_state = 'auto_redacted')"
        )
        conn.execute(
            "UPDATE sessions SET hold_state = 'auto_redacted' WHERE hold_state IS NULL"
        )

        # One origin history row per existing session.
        rows = conn.execute(
            "SELECT session_id, hold_state FROM sessions"
        ).fetchall()
        for row in rows:
            conn.execute(
                "INSERT INTO session_hold_history "
                "(history_id, session_id, from_state, to_state, embargo_until, "
                " changed_by, changed_at, reason) "
                "VALUES (?, ?, NULL, ?, NULL, 'migration', ?, 'schema migration backfill')",
                (str(uuid.uuid4()), row["session_id"], row["hold_state"], now),
            )

        # Flag the most-recently-active sessions for the Scanner to
        # backfill. Older sessions remain unflagged — users invoke
        # `scan --force` to pick them up explicitly.
        conn.execute(
            "UPDATE sessions SET findings_backfill_needed = 1 "
            "WHERE session_id IN ("
            "  SELECT session_id FROM sessions "
            "  ORDER BY COALESCE(end_time, '') DESC LIMIT ?"
            ")",
            (BACKFILL_WINDOW,),
        )

        conn.execute(f"PRAGMA user_version = {SECURITY_SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_session_identity_bridge(conn: sqlite3.Connection) -> None:
    """Add `sessions.session_key` + partial index and advance version 2 → 3."""
    version_row = conn.execute("PRAGMA user_version").fetchone()
    version = version_row[0] if version_row else 0
    if version >= SESSION_IDENTITY_SCHEMA_VERSION:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN session_key TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_session_key "
            "ON sessions(session_key) WHERE session_key IS NOT NULL"
        )
        conn.execute(f"PRAGMA user_version = {SESSION_IDENTITY_SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_widened_message_model(conn: sqlite3.Connection) -> None:
    """Mark this DB as widened-message-aware. Advances PRAGMA user_version 3 → 4.

    Phase-2 C1. The widened message model lives in the per-session JSON
    blob — message dicts gain optional ``invocations`` / ``snippets`` /
    ``extra`` / ``author`` fields. Existing blobs remain valid because
    every reader treats the new fields as optional (``msg.get("invocations", [])``).

    The migration adds ``sessions.message_schema_version`` so future
    backfills can identify legacy-shape rows without re-parsing each
    blob. Existing rows are stamped with version 1 (legacy); rows
    written by widened-aware code use version 2. The column is not
    consulted by any current reader — it is a forward-compat marker.

    Idempotent. Re-running on a v4 DB is a no-op.
    """
    version_row = conn.execute("PRAGMA user_version").fetchone()
    version = version_row[0] if version_row else 0
    if version >= WIDENED_MESSAGE_SCHEMA_VERSION:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        try:
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN message_schema_version INTEGER DEFAULT 2"
            )
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise
        # Backfill existing rows with version 1 (legacy shape). New
        # inserts get the column default (2).
        conn.execute(
            "UPDATE sessions SET message_schema_version = 1 "
            "WHERE message_schema_version IS NULL OR message_schema_version = 2"
        )
        conn.execute(f"PRAGMA user_version = {WIDENED_MESSAGE_SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_hosted_submission_receipts(conn: sqlite3.Connection) -> None:
    """Add hosted research submission receipt fields. Advances v4 -> v5."""
    version_row = conn.execute("PRAGMA user_version").fetchone()
    version = version_row[0] if version_row else 0
    if version >= HOSTED_SUBMISSION_SCHEMA_VERSION:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        for col, col_type in [
            ("hosted_receipt_id", "TEXT"),
            ("hosted_status", "TEXT"),
            ("hosted_submission_url", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE shares ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise
        conn.execute(f"PRAGMA user_version = {HOSTED_SUBMISSION_SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_revision_tracking(conn: sqlite3.Connection) -> None:
    """Add trace/share revisions and advance the workbench schema v5 -> v6.

    Existing sessions are hashed from their stored blobs. Historical shares
    inherit that same revision as their selection baseline: completed shares
    therefore stay suppressed, while old drafts can detect a later append
    before export. Missing or unreadable blobs receive an explicit stable
    legacy baseline, copied to their historical share selections as well.
    """
    version_row = conn.execute("PRAGMA user_version").fetchone()
    version = version_row[0] if version_row else 0
    if version >= REVISION_TRACKING_SCHEMA_VERSION:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, col, col_type in [
            ("sessions", "content_revision", "TEXT"),
            ("share_sessions", "content_revision", "TEXT"),
            ("share_sessions", "replaces_revision", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise

        session_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        blob_path_expr = (
            "blob_path" if "blob_path" in session_columns else "NULL AS blob_path"
        )
        rows = conn.execute(
            f"SELECT session_id, {blob_path_expr} FROM sessions "
            "WHERE content_revision IS NULL"
        ).fetchall()
        for row in rows:
            blob = _read_blob_for_revision(row["session_id"], row["blob_path"])
            revision = (
                compute_content_revision(blob)
                if blob is not None
                else _legacy_content_revision(row["session_id"])
            )
            conn.execute(
                "UPDATE sessions SET content_revision = ? WHERE session_id = ?",
                (revision, row["session_id"]),
            )

        # Very old indexes also kept the latest share on sessions.share_id.
        # Restore any missing join rows before stamping historical baselines.
        if "share_id" in session_columns:
            conn.execute(
                "INSERT OR IGNORE INTO share_sessions "
                "(share_id, session_id, added_at) "
                "SELECT s.share_id, s.session_id, sh.created_at "
                "FROM sessions s JOIN shares sh ON sh.share_id = s.share_id "
                "WHERE s.share_id IS NOT NULL"
            )
        # Existing drafts also need a frozen selection baseline so a later
        # append is detected before export. Successful rows use the same value
        # as their initial post-upgrade uploaded baseline.
        conn.execute(
            "UPDATE share_sessions AS ss "
            "SET content_revision = ("
            "  SELECT s.content_revision FROM sessions s "
            "  WHERE s.session_id = ss.session_id"
            ") "
            "WHERE ss.content_revision IS NULL"
        )
        conn.execute(f"PRAGMA user_version = {REVISION_TRACKING_SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_auto_upload_foundation(conn: sqlite3.Connection) -> None:
    """Add automatic-upload state and advance the workbench schema v6 -> v7.

    Existing revisions start their 24-hour stability clock at migration time.
    This deliberately prevents an old append-only trace from becoming eligible
    immediately merely because it was indexed before stability was tracked.
    """
    version_row = conn.execute("PRAGMA user_version").fetchone()
    version = version_row[0] if version_row else 0
    if version >= AUTO_UPLOAD_FOUNDATION_SCHEMA_VERSION:
        return

    now = _now_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, col, col_type in [
            ("sessions", "revision_stable_since", "TEXT"),
            ("shares", "submission_channel", "TEXT"),
            ("shares", "enrollment_id", "TEXT"),
            ("shares", "client_submission_id", "TEXT"),
            ("shares", "authorization_revision", "INTEGER"),
            ("shares", "submission_state", "TEXT"),
            ("shares", "sealed_artifact_sha256", "TEXT"),
            ("shares", "sealed_artifact_path", "TEXT"),
            ("shares", "sealed_raw_fingerprints", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise

        conn.execute(
            "UPDATE sessions SET revision_stable_since = ? "
            "WHERE revision_stable_since IS NULL",
            (now,),
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS auto_upload_enrollment (
                singleton_id                    INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                mode                            TEXT NOT NULL CHECK (mode IN ('off', 'enabled', 'paused')),
                health                          TEXT NOT NULL CHECK (health IN ('ready', 'action_required', 'retrying')),
                generation                      INTEGER NOT NULL CHECK (generation >= 1),
                enrolled_at                     TEXT NOT NULL,
                client_enrollment_id            TEXT NOT NULL,
                enrolled_sources_json           TEXT NOT NULL,
                enrolled_projects_json          TEXT NOT NULL,
                server_enrollment_id            TEXT,
                authorization_revision          INTEGER,
                recurring_authorization_version TEXT,
                retention_version               TEXT,
                egress_profile_hash             TEXT,
                hook_targets_json               TEXT NOT NULL DEFAULT '[]',
                claude_hook_observed_at         TEXT,
                codex_hook_observed_at          TEXT,
                last_completed_at               TEXT,
                next_retry_at                   TEXT,
                consecutive_failures            INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
                last_result_code                TEXT,
                last_result_count               INTEGER,
                last_receipt_reference          TEXT,
                current_run_id                  TEXT,
                current_run_stage               TEXT,
                revocation_pending              INTEGER NOT NULL DEFAULT 0 CHECK (revocation_pending IN (0, 1)),
                updated_at                      TEXT NOT NULL
            )"""
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_shares_client_submission_id "
            "ON shares(client_submission_id) "
            "WHERE client_submission_id IS NOT NULL"
        )
        conn.execute(
            f"PRAGMA user_version = {AUTO_UPLOAD_FOUNDATION_SCHEMA_VERSION}"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_auto_upload_recovery_metadata(conn: sqlite3.Connection) -> None:
    """Advance v7 -> v8 with hook observations and sealed raw fingerprints.

    The current fresh-install schema already contains these columns. Keeping
    this as a separately gated migration also upgrades indexes created by an
    earlier v7 build, where ``CREATE TABLE IF NOT EXISTS`` cannot add columns
    to existing tables.
    """
    version_row = conn.execute("PRAGMA user_version").fetchone()
    version = version_row[0] if version_row else 0
    if version >= AUTO_UPLOAD_SCHEMA_VERSION:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        for column in ("claude_hook_observed_at", "codex_hook_observed_at"):
            try:
                conn.execute(
                    f"ALTER TABLE auto_upload_enrollment ADD COLUMN {column} TEXT"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise
        try:
            conn.execute(
                "ALTER TABLE shares ADD COLUMN sealed_raw_fingerprints TEXT"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc):
                raise
        conn.execute(f"PRAGMA user_version = {AUTO_UPLOAD_SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_bundles_to_shares(conn: sqlite3.Connection) -> None:
    """One-time rename of bundles→shares, bundle_sessions→share_sessions, bundle_id→share_id.

    `ALTER TABLE bundles RENAME TO shares` only renames the table — the
    `bundle_id` column stays put, so subsequent inserts that reference
    `share_id` fail. We use the table-recreate pattern: build new tables
    with the proper schema, copy rows, drop the old tables. Gated on
    PRAGMA user_version so we only run once.

    We also recreate `sessions` even though `ALTER TABLE ... RENAME COLUMN
    bundle_id TO share_id` works in SQLite 3.25+, because once `bundles`
    is dropped, sessions' stored CREATE SQL still contains
    `REFERENCES bundles(bundle_id)` — which becomes a dangling FK.
    Recreating with an INSERT-SELECT preserves all dynamically-added
    ALTER columns from earlier versions.
    """
    version_row = conn.execute("PRAGMA user_version").fetchone()
    version = version_row[0] if version_row else 0
    if version >= 1:
        return

    # If the bundles table doesn't exist yet, this is a fresh install — just
    # bump the version and let SCHEMA_SQL create the new tables.
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='bundles'"
    ).fetchone()
    if existing is None:
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        return

    logger.info("Migrating index DB: bundles → shares")
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")

        # 1. Recreate shares with share_id (match SCHEMA_SQL exactly).
        conn.execute(
            """CREATE TABLE shares (
                share_id        TEXT PRIMARY KEY,
                created_at      TEXT NOT NULL,
                session_count   INTEGER,
                status          TEXT DEFAULT 'draft',
                attestation     TEXT,
                submission_note TEXT,
                bundle_hash     TEXT,
                manifest        TEXT,
                shared_at       TEXT,
                gcs_uri         TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO shares ("
            "  share_id, created_at, session_count, status, attestation,"
            "  submission_note, bundle_hash, manifest, shared_at, gcs_uri"
            ") SELECT"
            "  bundle_id, created_at, session_count, status, attestation,"
            "  submission_note, bundle_hash, manifest, shared_at, gcs_uri"
            " FROM bundles"
        )
        conn.execute("DROP TABLE bundles")

        # 2. Recreate share_sessions (FK column is share_id).
        conn.execute(
            """CREATE TABLE share_sessions (
                share_id          TEXT NOT NULL REFERENCES shares(share_id),
                session_id        TEXT NOT NULL REFERENCES sessions(session_id),
                added_at          TEXT NOT NULL,
                content_revision  TEXT,
                replaces_revision TEXT,
                PRIMARY KEY (share_id, session_id)
            )"""
        )
        conn.execute(
            "INSERT INTO share_sessions (share_id, session_id, added_at) "
            "SELECT bundle_id, session_id, added_at FROM bundle_sessions"
        )
        conn.execute("DROP TABLE bundle_sessions")
        conn.execute("DROP INDEX IF EXISTS idx_bundle_sessions_session_id")

        # 3. Recreate sessions so the FK references shares(share_id) and the
        #    column is named share_id. We dynamically copy every column the
        #    existing sessions table has so earlier ALTER-added columns
        #    (ai_*, segment_*, etc.) survive.
        old_cols_info = conn.execute("PRAGMA table_info(sessions)").fetchall()
        old_col_names = [row[1] for row in old_cols_info]
        # Build SELECT column list that maps bundle_id -> share_id.
        select_cols = [
            "bundle_id AS share_id" if name == "bundle_id" else name
            for name in old_col_names
        ]
        target_col_names = [
            "share_id" if name == "bundle_id" else name for name in old_col_names
        ]
        # Rebuild CREATE TABLE by copying the existing schema but swapping the
        # bundle_id column for a share_id FK column. We start from the base
        # SCHEMA_SQL columns, then append any extra columns that exist on the
        # old table.
        base_col_defs = [
            "session_id         TEXT PRIMARY KEY",
            "project            TEXT NOT NULL",
            "source             TEXT NOT NULL",
            "model              TEXT",
            "start_time         TEXT",
            "end_time           TEXT",
            "duration_seconds   INTEGER",
            "git_branch         TEXT",
            "user_messages      INTEGER DEFAULT 0",
            "assistant_messages INTEGER DEFAULT 0",
            "tool_uses          INTEGER DEFAULT 0",
            "input_tokens       INTEGER DEFAULT 0",
            "output_tokens      INTEGER DEFAULT 0",
            "display_title      TEXT",
            "outcome_badge      TEXT",
            "value_badges       TEXT",
            "risk_badges        TEXT",
            "sensitivity_score  REAL DEFAULT 0.0",
            "task_type          TEXT",
            "files_touched      TEXT",
            "commands_run       TEXT",
            "review_status      TEXT DEFAULT 'new'",
            "selection_reason   TEXT",
            "reviewer_notes     TEXT",
            "reviewed_at        TEXT",
            "blob_path          TEXT",
            "raw_source_path    TEXT",
            "indexed_at         TEXT NOT NULL",
            "updated_at         TEXT",
            "share_id           TEXT REFERENCES shares(share_id)",
            "ai_quality_score   INTEGER",
            "ai_score_reason    TEXT",
            "ai_display_title   TEXT",
            "ai_failure_value_score INTEGER",
            "ai_recovery_labels     TEXT",
            "ai_failure_attribution TEXT",
            "ai_failure_modes       TEXT",
            "ai_learning_summary    TEXT",
            "ai_scorer_backend      TEXT",
            "ai_scorer_model        TEXT",
            "ai_rubric_git_sha      TEXT",
            "ai_scored_at           TEXT",
            "content_revision       TEXT",
        ]
        known_names = {
            "session_id", "project", "source", "model", "start_time",
            "end_time", "duration_seconds", "git_branch", "user_messages",
            "assistant_messages", "tool_uses", "input_tokens", "output_tokens",
            "display_title", "outcome_badge", "value_badges", "risk_badges",
            "sensitivity_score", "task_type", "files_touched", "commands_run",
            "review_status", "selection_reason", "reviewer_notes",
            "reviewed_at", "blob_path", "raw_source_path", "indexed_at",
            "updated_at", "share_id", "ai_quality_score", "ai_score_reason",
            "ai_display_title", "ai_failure_value_score", "ai_recovery_labels",
            "ai_failure_attribution", "ai_failure_modes", "ai_learning_summary",
            "ai_scorer_backend", "ai_scorer_model", "ai_rubric_git_sha",
            "ai_scored_at", "content_revision",
        }
        # Map column name -> declared type from the old table so we preserve
        # types for any columns not in the base schema.
        old_types = {row[1]: row[2] or "TEXT" for row in old_cols_info}
        extra_defs = []
        for name in target_col_names:
            if name in known_names:
                continue
            extra_defs.append(f"{name} {old_types.get(name, 'TEXT')}")
        col_defs = base_col_defs + extra_defs
        conn.execute(f"CREATE TABLE sessions_new (\n    {', '.join(col_defs)}\n)")

        # Only copy columns that exist in both old and new tables.
        new_cols_info = conn.execute("PRAGMA table_info(sessions_new)").fetchall()
        new_col_names = {row[1] for row in new_cols_info}
        copy_targets = []
        copy_sources = []
        for src_expr, tgt_name in zip(select_cols, target_col_names):
            if tgt_name in new_col_names:
                copy_targets.append(tgt_name)
                copy_sources.append(src_expr)
        conn.execute(
            f"INSERT INTO sessions_new ({', '.join(copy_targets)})"
            f" SELECT {', '.join(copy_sources)} FROM sessions"
        )
        conn.execute("DROP TABLE sessions")
        conn.execute("ALTER TABLE sessions_new RENAME TO sessions")

        conn.execute("PRAGMA user_version = 1")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.execute("PRAGMA foreign_keys = ON")
        raise
    conn.execute("PRAGMA foreign_keys = ON")


def _flatten_transcript(session: dict[str, Any]) -> str:
    """Extract all message content and tool I/O as plain text for FTS indexing."""
    parts: list[str] = []
    for msg in session.get("messages", []):
        role = msg.get("role", "")
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    # Text blocks
                    text = block.get("text")
                    if text:
                        parts.append(text)
                    # Tool use input
                    tool_input = block.get("input")
                    if isinstance(tool_input, dict):
                        for v in tool_input.values():
                            if isinstance(v, str):
                                parts.append(v)
                    elif isinstance(tool_input, str):
                        parts.append(tool_input)
                    # Tool result output
                    output = block.get("output")
                    if isinstance(output, str):
                        parts.append(output)
        # Handle clawjournal's parsed format: tool uses stored as dicts with "tool" key
        tool = msg.get("tool")
        if tool:
            inp = msg.get("input")
            if isinstance(inp, dict):
                for v in inp.values():
                    if isinstance(v, str):
                        parts.append(v)
            out = msg.get("output")
            if isinstance(out, str):
                parts.append(out)
    return "\n".join(parts)


def _with_legacy_bundle_alias(item: dict[str, Any]) -> dict[str, Any]:
    """Expose bundle_id as a compatibility alias for share_id."""
    if "share_id" in item and "bundle_id" not in item:
        item["bundle_id"] = item["share_id"]
    return item


def _dedupe_strings(values: list[str]) -> list[str]:
    """Drop empty/duplicate strings while preserving first-seen order."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _derive_session_key_from_source(
    source: Any, raw_source_path: Any
) -> str | None:
    """Best-effort `event_sessions.session_key` derivation from workbench provenance."""
    if not isinstance(source, str) or not isinstance(raw_source_path, str):
        return None
    normalized_source_path = raw_source_path.strip()
    if not normalized_source_path:
        return None

    if source == "codex":
        return f"codex:{normalized_source_path}"
    if source == "openclaw":
        return f"openclaw:{normalized_source_path}"
    if source != "claude":
        return None

    path = Path(normalized_source_path)
    if ".claude" in path.parts and path.suffix == ".jsonl":
        la_key = _derive_local_agent_claude_session_key(path)
        if la_key is not None:
            return la_key
        # Real native paths (~/.claude/projects/<proj>/<uuid>.jsonl) also
        # contain `.claude`; when the local-agent wrapper probe fails, fall
        # through to the stem-based native derivation below.

    if path.suffix == ".jsonl":
        project_dir_name = path.parent.name
        session_id = path.stem
    else:
        project_dir_name = path.parent.name
        session_id = path.name

    if not project_dir_name or not session_id:
        return None
    return f"claude:{project_dir_name}:{session_id}"


def _derive_local_agent_claude_session_key(path: Path) -> str | None:
    """Recover the Claude local-agent session key from a nested transcript path."""
    try:
        session_dir = path.parents[3]
    except IndexError:
        return None

    wrapper_path = session_dir.with_suffix(".json")
    if not wrapper_path.is_file():
        return None

    # Deferred import to avoid a workbench↔capture cycle. We reach into two
    # private helpers in `clawjournal.capture.discovery` so the workbench
    # backfill derives exactly the same `session_key` the events-layer
    # capture adapter writes. If those helpers are renamed, update this
    # call site in lockstep or the derivation silently diverges.
    from clawjournal.capture import discovery

    wrapper = discovery._load_local_agent_wrapper(wrapper_path)
    if wrapper is None:
        return None
    cli_session_id = wrapper.get("cliSessionId")
    if not isinstance(cli_session_id, str) or cli_session_id != path.stem:
        return None
    workspace_key = discovery._workspace_key_from_wrapper(wrapper, session_dir)
    return f"claude:{workspace_key}:{cli_session_id}"


def _lookup_event_session_key(
    conn: sqlite3.Connection, raw_source_path: Any
) -> str | None:
    if not isinstance(raw_source_path, str) or not raw_source_path.strip():
        return None
    row = conn.execute(
        """
        SELECT event_sessions.session_key
          FROM event_sessions
          JOIN events ON events.session_id = event_sessions.id
         WHERE events.source_path = ?
         ORDER BY events.id
         LIMIT 1
        """,
        (raw_source_path,),
    ).fetchone()
    return None if row is None else str(row["session_key"])


def backfill_session_keys(conn: sqlite3.Connection) -> int:
    """Populate missing `sessions.session_key` values from events or path derivation.

    Safe to call on a fresh workbench DB where no ``events ingest`` has run yet —
    ``event_sessions`` / ``events`` are ensured here, so ``_lookup_event_session_key``
    degrades to an empty query and the backfill falls through to path derivation.
    """
    from clawjournal.events.schema import ensure_schema as ensure_event_schema

    ensure_event_schema(conn)
    rows = conn.execute(
        """
        SELECT session_id, source, raw_source_path
          FROM sessions
         WHERE session_key IS NULL
           AND source IN ('claude', 'codex', 'openclaw')
        """
    ).fetchall()
    if not rows:
        return 0

    updates: list[tuple[str, str]] = []
    for row in rows:
        session_key = _lookup_event_session_key(
            conn, row["raw_source_path"]
        ) or _derive_session_key_from_source(
            row["source"], row["raw_source_path"]
        )
        if session_key is not None:
            updates.append((session_key, row["session_id"]))

    if not updates:
        return 0

    with conn:
        cursor = conn.executemany(
            """
            UPDATE sessions
               SET session_key = ?
             WHERE session_id = ?
               AND session_key IS NULL
            """,
            updates,
        )
    # `cursor.rowcount` reflects rows actually affected by the guarded UPDATE
    # (concurrent backfills can see the same NULL candidates and race; the
    # `session_key IS NULL` guard keeps the data correct but `len(updates)`
    # would over-report in that case).
    return max(cursor.rowcount, 0)


def session_matches_excluded_projects(
    session: dict[str, Any],
    excluded_projects: list[str] | None = None,
) -> bool:
    """Return True when a session belongs to an excluded project."""
    if not excluded_projects:
        return False

    project = session.get("project")
    source = session.get("source")
    if not isinstance(project, str) or not project:
        return False

    candidates = {project}
    if isinstance(source, str) and source and ":" not in project:
        candidates.add(f"{source}:{project}")
    if any(candidate in excluded_projects for candidate in candidates):
        return True

    # Pre-basename Claude config entries looked like claude:path-to-project.
    # Keep them protective after sessions are re-keyed to claude:project.
    if project.startswith("claude:"):
        basename = project.removeprefix("claude:")
        if basename and not basename.startswith(("cowork/", "~")):
            legacy_suffix = f"-{basename}"
            for excluded in excluded_projects:
                if not isinstance(excluded, str):
                    continue
                legacy_name = excluded.removeprefix("claude:")
                if legacy_name != basename and legacy_name.endswith(legacy_suffix):
                    return True

    return False


def get_effective_share_settings(
    conn: sqlite3.Connection,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge config values with workbench policies for share/export operations."""
    resolved = dict(load_config()) if config is None else dict(config)

    custom_strings = list(resolved.get("redact_strings", []) or [])
    extra_usernames = list(resolved.get("redact_usernames", []) or [])
    allowlist_entries = list(resolved.get("allowlist_entries", []) or [])
    excluded_projects = list(
        normalize_excluded_project_names(resolved.get("excluded_projects", []) or [])
    )
    blocked_domains: list[str] = []

    for policy in get_policies(conn):
        value = policy.get("value")
        if not isinstance(value, str) or not value.strip():
            continue
        policy_type = policy.get("policy_type")
        if policy_type == "redact_string":
            custom_strings.append(value)
        elif policy_type == "redact_username":
            extra_usernames.append(value)
        elif policy_type == "exclude_project":
            excluded_projects.extend(normalize_excluded_project_names([value]))
        elif policy_type == "block_domain":
            blocked_domains.append(value)

    return {
        "custom_strings": _dedupe_strings(custom_strings),
        "extra_usernames": _dedupe_strings(extra_usernames),
        "allowlist_entries": allowlist_entries,
        "excluded_projects": _dedupe_strings(excluded_projects),
        "blocked_domains": _dedupe_strings(blocked_domains),
        "ai_pii_review_enabled": bool(resolved.get("ai_pii_review_enabled", False)),
        "source_scope": resolved.get("source"),
        "source_filter": source_scope_sources(resolved.get("source")),
    }


def _compile_blocked_domain_pattern(domain: str) -> re.Pattern[str] | None:
    """Compile a block-domain rule such as '*.internal' into a regex."""
    normalized = domain.strip().lower()
    if not normalized:
        return None
    if normalized.startswith("*."):
        suffix = normalized[2:].strip(".")
        if not suffix:
            return None
        pattern = rf"\b(?:[a-z0-9-]+\.)+{re.escape(suffix)}\b"
    else:
        pattern = rf"\b{re.escape(normalized)}\b"
    return re.compile(pattern, re.IGNORECASE)


def _transform_nested_strings(value: Any, transform) -> Any:
    """Apply a string transform recursively to dict/list structures."""
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, dict):
        return {k: _transform_nested_strings(v, transform) for k, v in value.items()}
    if isinstance(value, list):
        return [_transform_nested_strings(item, transform) for item in value]
    return value


def _redact_blocked_domains_in_value(
    value: Any,
    patterns: list[re.Pattern[str]],
    *,
    field: str,
    message_index: int | None = None,
    tool_field: str | None = None,
) -> tuple[Any, int, list[dict[str, Any]]]:
    """Apply block-domain rules to a string/dict/list value."""
    if isinstance(value, str):
        total = 0
        log: list[dict[str, Any]] = []
        updated = value
        for pattern in patterns:
            matches: list[str] = []

            def _replace(match: re.Match[str]) -> str:
                matches.append(match.group(0))
                return "[REDACTED_DOMAIN]"

            updated = pattern.sub(_replace, updated)
            total += len(matches)
            for match_text in matches:
                entry: dict[str, Any] = {
                    "type": "blocked_domain",
                    "confidence": 1.0,
                    "original_length": len(match_text),
                    "field": field,
                }
                if message_index is not None:
                    entry["message_index"] = message_index
                if tool_field is not None:
                    entry["tool_field"] = tool_field
                log.append(entry)
        return updated, total, log

    if isinstance(value, dict):
        total = 0
        log: list[dict[str, Any]] = []
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[key], count, entries = _redact_blocked_domains_in_value(
                item,
                patterns,
                field=field,
                message_index=message_index,
                tool_field=tool_field,
            )
            total += count
            log.extend(entries)
        return out, total, log

    if isinstance(value, list):
        total = 0
        log: list[dict[str, Any]] = []
        out_list: list[Any] = []
        for item in value:
            redacted, count, entries = _redact_blocked_domains_in_value(
                item,
                patterns,
                field=field,
                message_index=message_index,
                tool_field=tool_field,
            )
            out_list.append(redacted)
            total += count
            log.extend(entries)
        return out_list, total, log

    return value, 0, []


def _redact_custom_strings_in_value(
    value: Any,
    custom_strings: list[str],
) -> tuple[Any, int]:
    """Apply custom-string redactions recursively without running engine scans."""
    from ..redaction.secrets import redact_custom_strings

    if isinstance(value, str):
        return redact_custom_strings(value, custom_strings)
    if isinstance(value, dict):
        total = 0
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[key], count = _redact_custom_strings_in_value(item, custom_strings)
            total += count
        return out, total
    if isinstance(value, list):
        total = 0
        out_list: list[Any] = []
        for item in value:
            redacted, count = _redact_custom_strings_in_value(item, custom_strings)
            out_list.append(redacted)
            total += count
        return out_list, total
    return value, 0


def _load_finding_decisions(
    conn: sqlite3.Connection,
    session_id: str,
) -> dict[str, str]:
    rows = conn.execute(
        "SELECT entity_hash, status FROM findings WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    return {row["entity_hash"]: row["status"] for row in rows}


def _redaction_log_entry(
    *,
    type_name: str,
    confidence: float,
    original_length: int,
    field: str,
    message_index: int | None = None,
    tool_field: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": type_name,
        "confidence": confidence,
        "original_length": original_length,
        "field": field,
    }
    if message_index is not None:
        entry["message_index"] = message_index
    if tool_field is not None:
        entry["tool_field"] = tool_field
    return entry


def _build_deterministic_redaction_log(
    conn: sqlite3.Connection,
    session: dict[str, Any],
    *,
    user_allowlist: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build a metadata-only log from the findings-backed redaction substrate."""
    from ..findings import hash_entity
    from ..redaction.pii import _dedupe_overlapping_pii, scan_text_for_pii
    from ..redaction.secrets import _dedupe_overlapping_matches, _iter_text_locations, scan_text
    from ..redaction.trufflehog import scan_session_for_trufflehog_findings

    session_id = str(session.get("session_id") or "")
    if not session_id:
        return []

    decisions = _load_finding_decisions(conn, session_id)
    log: list[dict[str, Any]] = []

    for text, field, msg_idx, tool_field, _wk, _wkey in _iter_text_locations(session):
        for match in _dedupe_overlapping_matches(
            scan_text(text, user_allowlist=user_allowlist),
        ):
            if decisions.get(hash_entity(match["match"])) == "ignored":
                continue
            log.append(_redaction_log_entry(
                type_name=match["type"],
                confidence=match["confidence"],
                original_length=match["end"] - match["start"],
                field=field,
                message_index=msg_idx,
                tool_field=tool_field,
            ))
        for match in _dedupe_overlapping_pii(
            scan_text_for_pii(text, user_allowlist=user_allowlist),
        ):
            if decisions.get(hash_entity(match["match"])) == "ignored":
                continue
            log.append(_redaction_log_entry(
                type_name=match["type"],
                confidence=match["confidence"],
                original_length=match["end"] - match["start"],
                field=field,
                message_index=msg_idx,
                tool_field=tool_field,
            ))

    try:
        for finding in scan_session_for_trufflehog_findings(
            session,
            user_allowlist=user_allowlist,
        ):
            if decisions.get(hash_entity(finding.entity_text)) == "ignored":
                continue
            log.append(_redaction_log_entry(
                type_name=f"trufflehog_{finding.rule.lower()}",
                confidence=finding.confidence,
                original_length=finding.length,
                field=finding.field,
                message_index=finding.message_index,
                tool_field=finding.tool_field,
            ))
    except Exception:  # noqa: BLE001 — preview/export should fail soft on engine issues
        logger.warning("TruffleHog log build failed", exc_info=True)

    return log


def _apply_to_ai_text(
    session: dict[str, Any],
    transform: Callable[[str, str], str],
) -> None:
    """Apply ``transform(text, field_label) -> text`` to every judge-generated free-text field.

    The judge sees only anonymized message content, but its outputs
    (summary, evidence paraphrases, learning summary) can still contain
    project, lab, or dataset names. These fields end up in the export
    bundle, so they must pass through the same share-time redaction
    stack as ``display_title``.

    ``field_label`` is a precise dotted path so callers that build
    redaction-log entries can attribute hits — examples:
    ``"ai_learning_summary"``, ``"ai_scoring_detail.reason"``,
    ``"ai_scoring_detail.ai_failure_evidence[2]"``.

    The walked field set is defined by the ``AI_TEXT_*`` constants in
    ``clawjournal.redaction.secrets`` — that module is the single source
    of truth so adding a judge-emitted field only requires updating one
    list.
    """
    from ..redaction.secrets import (
        AI_TEXT_DETAIL_FIELD,
        AI_TEXT_DETAIL_LIST_FIELDS,
        AI_TEXT_DETAIL_STR_FIELDS,
        AI_TEXT_TOP_FIELD,
    )

    top_value = session.get(AI_TEXT_TOP_FIELD)
    if isinstance(top_value, str) and top_value:
        session[AI_TEXT_TOP_FIELD] = transform(top_value, AI_TEXT_TOP_FIELD)

    raw = session.get(AI_TEXT_DETAIL_FIELD)
    if not isinstance(raw, str) or not raw:
        return
    try:
        detail = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(detail, dict):
        return

    for field in AI_TEXT_DETAIL_STR_FIELDS:
        value = detail.get(field)
        if isinstance(value, str) and value:
            detail[field] = transform(value, f"{AI_TEXT_DETAIL_FIELD}.{field}")

    for list_field in AI_TEXT_DETAIL_LIST_FIELDS:
        items = detail.get(list_field)
        if not isinstance(items, list):
            continue
        new_items: list[Any] = []
        for idx, item in enumerate(items):
            if isinstance(item, str) and item:
                label = f"{AI_TEXT_DETAIL_FIELD}.{list_field}[{idx}]"
                new_items.append(transform(item, label))
            else:
                new_items.append(item)
        detail[list_field] = new_items

    session[AI_TEXT_DETAIL_FIELD] = json.dumps(detail)


def apply_share_redactions(
    conn: sqlite3.Connection,
    session: dict[str, Any],
    *,
    custom_strings: list[str] | None = None,
    user_allowlist: list[dict[str, Any]] | None = None,
    extra_usernames: list[str] | None = None,
    blocked_domains: list[str] | None = None,
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    """Apply the full share/export redaction pipeline to a session."""
    from ..redaction.anonymizer import Anonymizer
    from ..redaction.normalize import strip_terminal_control_sequences
    from ..redaction.secrets import apply_findings_to_blob

    total_redactions = 0
    redaction_log: list[dict[str, Any]] = []

    # Normalize terminal control sequences (ANSI colors, cursor moves, stray
    # control bytes) out of all text up front. They are rendering noise, not
    # content: stripping them keeps shared bundles clean, lets downstream
    # redaction patterns match cleanly, and stops TruffleHog's broad detectors
    # from flagging them as false positives. This is normalization, not
    # redaction, so it is not counted in total_redactions.
    for field in ("display_title", "project", "git_branch"):
        if session.get(field):
            session[field] = _transform_nested_strings(
                session[field], strip_terminal_control_sequences
            )
    for msg in session.get("messages", []):
        for field in ("content", "thinking"):
            if msg.get(field):
                msg[field] = _transform_nested_strings(
                    msg[field], strip_terminal_control_sequences
                )
        for tool_use in msg.get("tool_uses", []):
            for tool_field in ("input", "output"):
                if tool_use.get(tool_field):
                    tool_use[tool_field] = _transform_nested_strings(
                        tool_use[tool_field], strip_terminal_control_sequences
                    )
        # Widened message model (author / invocations / snippets / extra) —
        # these ship in the export too, so normalize them with the same
        # recursive walk the secrets stage uses (iter_widened_text_locations).
        for widened_field in ("author", "invocations", "snippets", "extra"):
            if msg.get(widened_field):
                msg[widened_field] = _transform_nested_strings(
                    msg[widened_field], strip_terminal_control_sequences
                )
    _apply_to_ai_text(session, lambda text, _label: strip_terminal_control_sequences(text))

    if custom_strings:
        custom_total = 0
        for field in ("display_title", "project", "git_branch"):
            if session.get(field):
                session[field], count = _redact_custom_strings_in_value(
                    session[field],
                    custom_strings,
                )
                custom_total += count

        for msg in session.get("messages", []):
            for field in ("content", "thinking"):
                if msg.get(field):
                    msg[field], count = _redact_custom_strings_in_value(
                        msg[field],
                        custom_strings,
                    )
                    custom_total += count
            for tool_use in msg.get("tool_uses", []):
                for tool_field in ("input", "output"):
                    if tool_use.get(tool_field):
                        tool_use[tool_field], count = _redact_custom_strings_in_value(
                            tool_use[tool_field],
                            custom_strings,
                        )
                        custom_total += count

        custom_ai_count = [0]

        def _custom_transform(text: str, _label: str) -> str:
            new_text, n = _redact_custom_strings_in_value(text, custom_strings)
            custom_ai_count[0] += n
            return new_text

        _apply_to_ai_text(session, _custom_transform)
        custom_total += custom_ai_count[0]

        total_redactions += custom_total

    domain_patterns = [
        pattern
        for pattern in (_compile_blocked_domain_pattern(domain) for domain in (blocked_domains or []))
        if pattern is not None
    ]
    if domain_patterns:
        domain_total = 0
        domain_log: list[dict[str, Any]] = []

        for field in ("display_title", "project", "git_branch"):
            if session.get(field):
                session[field], count, entries = _redact_blocked_domains_in_value(
                    session[field],
                    domain_patterns,
                    field=field,
                )
                domain_total += count
                domain_log.extend(entries)

        for msg_idx, msg in enumerate(session.get("messages", [])):
            for field in ("content", "thinking"):
                if msg.get(field):
                    msg[field], count, entries = _redact_blocked_domains_in_value(
                        msg[field],
                        domain_patterns,
                        field=field,
                        message_index=msg_idx,
                    )
                    domain_total += count
                    domain_log.extend(entries)
            for tool_use in msg.get("tool_uses", []):
                for tool_field in ("input", "output"):
                    if tool_use.get(tool_field):
                        tool_use[tool_field], count, entries = _redact_blocked_domains_in_value(
                            tool_use[tool_field],
                            domain_patterns,
                            field=f"tool_{tool_field}",
                            message_index=msg_idx,
                            tool_field=tool_field,
                        )
                        domain_total += count
                        domain_log.extend(entries)

        domain_ai_count = [0]

        def _domain_transform(text: str, label: str) -> str:
            new_text, n, entries = _redact_blocked_domains_in_value(
                text,
                domain_patterns,
                field=label,
            )
            domain_ai_count[0] += n
            domain_log.extend(entries)
            return new_text

        _apply_to_ai_text(session, _domain_transform)
        domain_total += domain_ai_count[0]

        total_redactions += domain_total
        redaction_log.extend(domain_log)

    anonymizer = Anonymizer(extra_usernames=extra_usernames)
    for field in ("display_title", "project", "git_branch"):
        if session.get(field):
            session[field] = _transform_nested_strings(session[field], anonymizer.text)
    for msg in session.get("messages", []):
        for field in ("content", "thinking"):
            if msg.get(field):
                msg[field] = _transform_nested_strings(msg[field], anonymizer.text)
        for tool_use in msg.get("tool_uses", []):
            for tool_field in ("input", "output"):
                if tool_use.get(tool_field):
                    tool_use[tool_field] = _transform_nested_strings(
                        tool_use[tool_field],
                        anonymizer.text,
                    )

    _apply_to_ai_text(session, lambda text, _label: anonymizer.text(text))

    session_id = str(session.get("session_id") or "")
    if not session_id:
        # Silent no-op here would ship an un-redacted blob because all
        # three deterministic engines route through the findings table,
        # which is keyed by session_id. Fail loud instead — callers
        # should never hand us a session stripped of its identifier.
        raise ValueError(
            "apply_share_redactions requires session['session_id']; "
            "got an empty/missing value. The findings-backed engines "
            "cannot attribute decisions without it."
        )

    redaction_log.extend(
        _build_deterministic_redaction_log(
            conn,
            session,
            user_allowlist=user_allowlist,
        ),
    )
    session, deterministic_total = apply_findings_to_blob(
        session,
        conn,
        session_id,
        user_allowlist=user_allowlist,
    )
    total_redactions += deterministic_total

    # TruffleHog acts as another detection+redaction engine in the
    # pipeline through apply_findings_to_blob, so the share-time redaction
    # step respects the same ignored/open decision substrate as the other
    # deterministic engines. The later Package-step gate still re-scans the
    # merged output independently before export/upload.
    #
    # Known tradeoff: ``redaction_log`` is built from a single pre-apply
    # scan (see ``_build_deterministic_redaction_log``); the apply step
    # runs up to ``max_passes=3`` passes that can surface secrets
    # revealed by earlier replacements. In that rare case
    # ``total_redactions`` may exceed ``len(redaction_log)``. The UI
    # bucket counts come from the log and will undercount those
    # multi-pass finds; the Package-step gate is authoritative.

    return session, total_redactions, redaction_log


def _extract_files_touched(session: dict[str, Any]) -> list[str]:
    """Extract file paths from tool use inputs across all messages."""
    files: set[str] = set()
    for msg in session.get("messages", []):
        content = msg.get("content")
        blocks = []
        if isinstance(content, list):
            blocks = content
        # Also handle clawjournal parsed format
        if msg.get("tool"):
            blocks = [msg]

        for block in blocks:
            if not isinstance(block, dict):
                continue
            inp = block.get("input", {})
            if not isinstance(inp, dict):
                continue
            for key in ("file_path", "path", "file", "filename"):
                val = inp.get(key)
                if isinstance(val, str) and val.strip():
                    files.add(val.strip())
    return sorted(files)


def _extract_commands_run(session: dict[str, Any]) -> list[str]:
    """Extract shell commands from bash/shell tool uses."""
    commands: list[str] = []
    for msg in session.get("messages", []):
        content = msg.get("content")
        blocks = []
        if isinstance(content, list):
            blocks = content
        if msg.get("tool"):
            blocks = [msg]

        for block in blocks:
            if not isinstance(block, dict):
                continue
            tool_name = block.get("tool") or block.get("name", "")
            if tool_name not in ("bash", "shell", "terminal", "execute_command"):
                continue
            inp = block.get("input", {})
            if not isinstance(inp, dict):
                continue
            cmd = inp.get("command") or inp.get("cmd", "")
            if isinstance(cmd, str) and cmd.strip():
                commands.append(cmd.strip())
    return commands


def _compute_duration(session: dict[str, Any]) -> int | None:
    """Compute duration in seconds from start_time and end_time."""
    start = session.get("start_time")
    end = session.get("end_time")
    if not start or not end:
        return None
    try:
        start_dt = datetime.fromisoformat(str(start))
        end_dt = datetime.fromisoformat(str(end))
        delta = (end_dt - start_dt).total_seconds()
        if delta < 0:
            return None
        return int(delta)
    except (ValueError, TypeError):
        return None


def _generate_display_title(session: dict[str, Any]) -> str:
    """Generate a display title from the first user message, truncated."""
    # Prefer segment_title for child traces (already stripped of metadata)
    seg_title = session.get("segment_title")
    if seg_title:
        if len(seg_title) > 120:
            return seg_title[:117] + "..."
        return seg_title
    for msg in session.get("messages", []):
        role = msg.get("role", "")
        if role != "user":
            continue
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    text = block
                    break
                if isinstance(block, dict) and block.get("text"):
                    text = block["text"]
                    break
        text = text.strip()
        if text:
            # Truncate to first line, max 120 chars
            first_line = text.split("\n", 1)[0].strip()
            if len(first_line) > 120:
                return first_line[:117] + "..."
            return first_line
    return session.get("session_id", "untitled")


def _write_blob(session_id: str, session: dict[str, Any]) -> Path:
    """Write full session JSON to blob storage. Returns the blob file path."""
    BLOBS_DIR.mkdir(parents=True, exist_ok=True)
    blob_path = _blob_storage_path(session_id)
    with open(blob_path, "w", encoding="utf-8") as f:
        json.dump(session, f, default=str)
    return blob_path


def read_blob(
    session_id: str,
    *,
    log_errors: bool = True,
) -> dict[str, Any] | None:
    """Return the stored session blob as a dict, or None if missing/unreadable.

    Used by the findings backfill drain and share-time apply — they
    need the already-anonymized blob text to re-scan or re-apply
    without re-parsing from the source.
    """
    blob = _read_blob_for_revision(session_id, None)
    if blob is None and log_errors:
        logger.warning("Could not read blob for session %s", session_id)
    return blob


def _resolve_estimated_cost(
    existing: sqlite3.Row | None,
    *,
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    end_time: str | None,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float | None:
    """Choose which cost value to persist during session upsert.

    Completed sessions (with end_time) keep their first stored estimate so
    dashboard totals stay stable. Ongoing sessions recompute as they grow.
    """
    if existing is not None:
        preserved_cost = existing["estimated_cost_usd"]
        if preserved_cost is not None and end_time is not None:
            return preserved_cost

    return estimate_cost(
        model, input_tokens, output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
    )


def recompute_estimated_costs(conn: sqlite3.Connection) -> int:
    """Re-price every session's ``estimated_cost_usd`` from its stored token
    columns and the current pricing table. Returns the number of rows changed.

    This deliberately overrides the frozen-cost stability guarantee in
    :func:`_resolve_estimated_cost`, so it is only invoked from the explicit
    ``refresh-pricing`` CLI path — never on routine ``scan``/``serve``. Run it
    after a ``scan --force`` if you also need corrected token columns (e.g. after
    a parser cache-accounting fix).
    """
    rows = conn.execute(
        "SELECT session_id, model, input_tokens, output_tokens, "
        "cache_read_tokens, cache_creation_tokens, estimated_cost_usd "
        "FROM sessions"
    ).fetchall()
    changed = 0
    for r in rows:
        new_cost = estimate_cost(
            r["model"],
            r["input_tokens"] or 0,
            r["output_tokens"] or 0,
            cache_read_tokens=r["cache_read_tokens"] or 0,
            cache_creation_tokens=r["cache_creation_tokens"] or 0,
        )
        old_cost = r["estimated_cost_usd"]
        if new_cost is None and old_cost is None:
            continue
        if (
            new_cost is not None
            and old_cost is not None
            and abs(new_cost - old_cost) < 1e-9
        ):
            continue
        conn.execute(
            "UPDATE sessions SET estimated_cost_usd = ? WHERE session_id = ?",
            (new_cost, r["session_id"]),
        )
        changed += 1
    conn.commit()
    return changed


def upsert_sessions(
    conn: sqlite3.Connection,
    sessions: list[dict[str, Any]],
    *,
    stats: dict[str, int] | None = None,
) -> int:
    """Index parsed sessions into the database.

    Takes parsed session dicts (output of parser.parse_project_sessions).
    Stores metadata in sessions table, writes full session JSON to a
    cross-platform-safe path under BLOBS_DIR, and updates FTS index.

    Returns the count of new sessions inserted (sessions that did not
    previously exist in the index). When ``stats`` is provided, it is filled
    with ``inserted``, ``updated`` (content revision changed), and
    ``unchanged`` counts without changing the legacy integer return value.
    """
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    if stats is not None:
        stats.clear()
        stats.update(counts)
    if not sessions:
        return 0

    now = _now_iso()
    new_count = 0

    # Check FTS availability
    has_fts = _has_fts(conn)

    for session in sessions:
        session_id = session.get("session_id")
        if not session_id:
            continue

        project = session.get("project", "")
        source = session.get("source", "")
        if not project or not source:
            continue

        session_key = _derive_session_key_from_source(
            source, session.get("raw_source_path")
        )
        session_stats = session.get("stats", {})
        duration = _compute_duration(session)
        content_revision = compute_content_revision(session)

        # Compute badges and signals
        badges = compute_all_badges(session)
        display_title = badges["display_title"]
        files = badges["files_touched"]
        commands = badges["commands_run"]

        # Skip sessions that are just slash commands (not real traces)
        if display_title.startswith("/") and " " not in display_title.strip():
            continue

        # The sessions row is a plaintext surface — list views, search,
        # API responses all return `display_title` directly. Strip any
        # regex_secrets match before persisting so the body of a prompt
        # that happens to contain `ghp_...` doesn't leak into the DB.
        display_title, _, _ = redact_text(display_title)

        # Check if session already exists and capture fields we need to preserve
        existing = conn.execute(
            "SELECT session_id, review_status, reviewed_at, "
            "selection_reason, reviewer_notes, indexed_at, "
            "ai_quality_score, ai_score_reason, ai_scoring_detail, "
            "ai_display_title, ai_task_type, ai_outcome_badge, "
            "ai_value_badges, ai_risk_badges, "
            "ai_effort_estimate, ai_summary, "
            "share_id, session_key, parent_session_id, subagent_session_ids, "
            "estimated_cost_usd, end_time, content_revision "
            "FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        is_new = existing is None
        content_updated = (
            existing is not None
            and existing["content_revision"] != content_revision
        )

        # Avoid rewriting the blob/FTS record for a byte-equivalent transcript.
        # Metadata fields still flow through the SQL upsert below so parser
        # enrichment and ongoing cost estimates can refresh independently.
        metadata_updated = False
        if is_new or content_updated:
            blob_path = _write_blob(session_id, session)
        else:
            stored_blob_path = conn.execute(
                "SELECT blob_path FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()["blob_path"]
            blob_path = (
                Path(stored_blob_path)
                if stored_blob_path
                else _blob_storage_path(session_id)
            )
            stored_blob = _read_blob_for_revision(session_id, str(blob_path))
            metadata_updated = (
                stored_blob is None
                or _revision_json_value(stored_blob) != _revision_json_value(session)
            )
            if metadata_updated:
                blob_path = _write_blob(session_id, session)

        # Delete old FTS entry before replacing.
        if has_fts and not is_new and (content_updated or metadata_updated):
            conn.execute(
                "DELETE FROM sessions_fts WHERE session_id = ?",
                (session_id,),
            )

        # Preserve review state, AI metadata, and linkage fields from the old row
        # before REPLACE deletes it. INSERT OR REPLACE deletes the
        # conflicting row first, so subqueries referencing the old row in
        # VALUES would find nothing.
        preserved_status = existing["review_status"] if not is_new else "new"
        preserved_reviewed_at = existing["reviewed_at"] if not is_new else None
        preserved_reason = existing["selection_reason"] if not is_new else None
        preserved_notes = existing["reviewer_notes"] if not is_new else None
        preserved_indexed_at = existing["indexed_at"] if not is_new else now
        preserved_ai_score = existing["ai_quality_score"] if not is_new else None
        preserved_ai_reason = existing["ai_score_reason"] if not is_new else None
        preserved_ai_detail = existing["ai_scoring_detail"] if not is_new else None
        preserved_ai_title = existing["ai_display_title"] if not is_new else None
        preserved_ai_task = existing["ai_task_type"] if not is_new else None
        preserved_ai_outcome = existing["ai_outcome_badge"] if not is_new else None
        preserved_ai_values = existing["ai_value_badges"] if not is_new else None
        preserved_ai_risks = existing["ai_risk_badges"] if not is_new else None
        preserved_ai_effort = existing["ai_effort_estimate"] if not is_new else None
        preserved_ai_summary = existing["ai_summary"] if not is_new else None
        preserved_share_id = existing["share_id"] if not is_new else None
        preserved_session_key = existing["session_key"] if not is_new else None
        preserved_parent_session_id = existing["parent_session_id"] if not is_new else None
        preserved_subagent_session_ids = existing["subagent_session_ids"] if not is_new else None

        # Compute estimated cost from model + token counts
        in_tok = session_stats.get("input_tokens", 0)
        out_tok = session_stats.get("output_tokens", 0)
        cache_read = session_stats.get("cache_read_tokens", 0)
        cache_create = session_stats.get("cache_creation_tokens", 0)
        cost = _resolve_estimated_cost(
            existing,
            model=session.get("model"),
            input_tokens=in_tok,
            output_tokens=out_tok,
            end_time=session.get("end_time"),
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_create,
        )

        # Non-destructive upsert. INSERT OR REPLACE would delete the
        # existing row first, which cascades through findings and
        # session_hold_history via ON DELETE CASCADE. ON CONFLICT DO
        # UPDATE changes the columns we want refreshed without
        # touching the row identity, leaving cascading children
        # intact. Fields we want preserved on update (review state,
        # AI metadata, linkage, hold state, findings_revision, etc.)
        # are simply absent from the SET clause.
        conn.execute(
            """INSERT INTO sessions (
                session_id, project, source, model, model_effort,
                start_time, end_time, duration_seconds,
                git_branch,
                user_messages, assistant_messages, tool_uses,
                input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens,
                display_title,
                outcome_badge, value_badges, risk_badges,
                sensitivity_score, task_type,
                files_touched, commands_run,
                blob_path, raw_source_path,
                session_key,
                indexed_at, updated_at,
                review_status,
                selection_reason, reviewer_notes, reviewed_at,
                ai_quality_score, ai_score_reason, ai_scoring_detail,
                ai_display_title, ai_task_type, ai_outcome_badge,
                ai_value_badges, ai_risk_badges,
                ai_effort_estimate, ai_summary,
                share_id,
                parent_session_id, subagent_session_ids, segment_index,
                segment_start_message, segment_end_message,
                segment_reason,
                client_origin, runtime_channel, outer_session_id,
                estimated_cost_usd,
                tool_counts, user_interrupts,
                hold_state, content_revision, revision_stable_since
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?,
                ?, ?, ?,
                ?, ?,
                ?, ?,
                ?,
                ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?,
                ?,
                ?, ?, ?,
                ?, ?,
                ?,
                ?, ?, ?,
                ?,
                ?, ?,
                'auto_redacted', ?, ?
            )
            ON CONFLICT(session_id) DO UPDATE SET
                project = excluded.project,
                source = excluded.source,
                model = excluded.model,
                model_effort = excluded.model_effort,
                start_time = excluded.start_time,
                end_time = excluded.end_time,
                duration_seconds = excluded.duration_seconds,
                git_branch = excluded.git_branch,
                user_messages = excluded.user_messages,
                assistant_messages = excluded.assistant_messages,
                tool_uses = excluded.tool_uses,
                input_tokens = excluded.input_tokens,
                output_tokens = excluded.output_tokens,
                cache_read_tokens = excluded.cache_read_tokens,
                cache_creation_tokens = excluded.cache_creation_tokens,
                display_title = excluded.display_title,
                outcome_badge = excluded.outcome_badge,
                value_badges = excluded.value_badges,
                risk_badges = excluded.risk_badges,
                sensitivity_score = excluded.sensitivity_score,
                task_type = excluded.task_type,
                files_touched = excluded.files_touched,
                commands_run = excluded.commands_run,
                blob_path = excluded.blob_path,
                raw_source_path = excluded.raw_source_path,
                session_key = COALESCE(excluded.session_key, session_key),
                updated_at = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN excluded.updated_at ELSE sessions.updated_at END,
                review_status = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN CASE
                        WHEN sessions.review_status = 'segmented' THEN 'segmented'
                        ELSE 'new'
                    END
                    ELSE sessions.review_status
                END,
                selection_reason = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.selection_reason END,
                reviewer_notes = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.reviewer_notes END,
                reviewed_at = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.reviewed_at END,
                ai_quality_score = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_quality_score END,
                ai_score_reason = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_score_reason END,
                ai_episode_quality = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_episode_quality END,
                ai_quality_tier = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_quality_tier END,
                ai_scoring_detail = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_scoring_detail END,
                ai_task_type = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_task_type END,
                ai_outcome_badge = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_outcome_badge END,
                ai_value_badges = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_value_badges END,
                ai_risk_badges = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_risk_badges END,
                ai_display_title = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_display_title END,
                ai_effort_estimate = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_effort_estimate END,
                ai_summary = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_summary END,
                ai_failure_value_score = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_failure_value_score END,
                ai_recovery_labels = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_recovery_labels END,
                ai_failure_attribution = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_failure_attribution END,
                ai_failure_modes = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_failure_modes END,
                ai_learning_summary = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_learning_summary END,
                ai_scorer_backend = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_scorer_backend END,
                ai_scorer_model = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_scorer_model END,
                ai_rubric_git_sha = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_rubric_git_sha END,
                ai_scored_at = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN NULL ELSE sessions.ai_scored_at END,
                parent_session_id = COALESCE(excluded.parent_session_id, parent_session_id),
                segment_index = excluded.segment_index,
                segment_start_message = excluded.segment_start_message,
                segment_end_message = excluded.segment_end_message,
                segment_reason = excluded.segment_reason,
                client_origin = excluded.client_origin,
                runtime_channel = excluded.runtime_channel,
                outer_session_id = excluded.outer_session_id,
                estimated_cost_usd = excluded.estimated_cost_usd,
                tool_counts = excluded.tool_counts,
                user_interrupts = excluded.user_interrupts,
                revision_stable_since = CASE
                    WHEN sessions.content_revision IS NOT excluded.content_revision
                    THEN excluded.revision_stable_since
                    ELSE sessions.revision_stable_since END,
                content_revision = excluded.content_revision
            """,
            (
                session_id, project, source, session.get("model"), session.get("model_effort"),
                session.get("start_time"), session.get("end_time"), duration,
                session.get("git_branch"),
                session_stats.get("user_messages", 0),
                session_stats.get("assistant_messages", 0),
                session_stats.get("tool_uses", 0),
                in_tok,
                out_tok,
                cache_read,
                cache_create,
                display_title,
                badges["outcome_badge"],
                json.dumps(badges["value_badges"]),
                json.dumps(badges["risk_badges"]),
                badges["sensitivity_score"],
                badges["task_type"],
                json.dumps(files),
                json.dumps(commands),
                str(blob_path),
                session.get("raw_source_path"),
                session_key or preserved_session_key,
                preserved_indexed_at,
                now,
                preserved_status,
                preserved_reason,
                preserved_notes,
                preserved_reviewed_at,
                preserved_ai_score,
                preserved_ai_reason,
                preserved_ai_detail,
                preserved_ai_title,
                preserved_ai_task,
                preserved_ai_outcome,
                preserved_ai_values,
                preserved_ai_risks,
                preserved_ai_effort,
                preserved_ai_summary,
                preserved_share_id,
                session.get("parent_session_id") or preserved_parent_session_id,
                preserved_subagent_session_ids,
                session.get("segment_index"),
                session.get("segment_message_range", [None, None])[0] if session.get("segment_message_range") else None,
                session.get("segment_message_range", [None, None])[1] if session.get("segment_message_range") else None,
                session.get("segment_reason"),
                session.get("client_origin"),
                session.get("runtime_channel"),
                session.get("outer_session_id"),
                cost,
                json.dumps(badges.get("tool_counts", {})) or None,
                session_stats.get("user_interrupts", 0),
                content_revision,
                now,
            ),
        )

        # For brand-new sessions, stamp an origin hold-history row
        # in the same implicit transaction so every session is
        # guaranteed to have a timeline row from the moment it
        # exists (see Decision 18 + §session_hold_history).
        if is_new:
            conn.execute(
                "INSERT INTO session_hold_history "
                "(history_id, session_id, from_state, to_state, embargo_until, "
                " changed_by, changed_at, reason) "
                "VALUES (?, ?, NULL, 'auto_redacted', NULL, 'auto', ?, NULL)",
                (str(uuid.uuid4()), session_id, now),
            )

        # Insert FTS entry
        if has_fts and (is_new or content_updated or metadata_updated):
            transcript = _flatten_transcript(session)
            conn.execute(
                "INSERT INTO sessions_fts("
                "session_id, display_title, transcript_text, files_touched, commands_run) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    session_id,
                    display_title,
                    transcript,
                    " ".join(files),
                    " ".join(commands),
                ),
            )

        if is_new:
            new_count += 1
            counts["inserted"] += 1
        elif content_updated:
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1

    conn.commit()
    if stats is not None:
        stats.update(counts)
    return new_count


def _has_fts(conn: sqlite3.Connection) -> bool:
    """Check if the FTS virtual table exists."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions_fts'"
    ).fetchone()
    return row is not None


def _build_start_time_where(
    *,
    start: str | None = None,
    end: str | None = None,
    base_clauses: list[str] | None = None,
) -> tuple[str, list[Any]]:
    """Build a reusable WHERE clause for optional start_time date filtering."""
    clauses = list(base_clauses or [])
    params: list[Any] = []
    if start:
        clauses.append("DATE(start_time) >= ?")
        params.append(start)
    if end:
        clauses.append("DATE(start_time) <= ?")
        params.append(end)
    if not clauses:
        return "", []
    return f" WHERE {' AND '.join(clauses)}", params


def query_sessions(
    conn: sqlite3.Connection,
    *,
    status: str | list[str] | tuple[str, ...] | None = None,
    source: str | list[str] | tuple[str, ...] | None = None,
    project: str | None = None,
    task_type: str | None = None,
    recovery_label: str | None = None,
    failure_attribution: str | None = None,
    failure_mode: str | None = None,
    search_text: str | None = None,
    sort: str = "ai_failure_value_score",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
    exclude_segmented_parents: bool = False,
) -> list[dict[str, Any]]:
    """Query sessions with optional filters.

    If search_text is provided and FTS is available, joins with the FTS
    index. Returns a list of dicts containing metadata (no messages).
    """
    # Validate sort column to prevent SQL injection
    allowed_sort_columns = {
        "start_time", "end_time", "indexed_at", "updated_at",
        "project", "source", "model", "model_effort", "review_status", "task_type",
        "user_messages", "assistant_messages", "tool_uses",
        "input_tokens", "output_tokens", "duration_seconds",
        "sensitivity_score", "ai_quality_score", "ai_failure_value_score",
    }
    if sort not in allowed_sort_columns:
        sort = "start_time"
    if order.lower() not in ("asc", "desc"):
        order = "desc"

    params: list[Any] = []
    where_clauses: list[str] = []

    if search_text and _has_fts(conn):
        # FTS join query
        base = (
            "SELECT s.* FROM sessions s "
            "JOIN sessions_fts f ON s.session_id = f.session_id "
            "WHERE sessions_fts MATCH ?"
        )
        params.append(search_text)
    else:
        base = "SELECT * FROM sessions s WHERE 1=1"

    if status is not None:
        if isinstance(status, (list, tuple)):
            values = [s for s in status if s]
            if values:
                placeholders = ",".join("?" for _ in values)
                where_clauses.append(f"s.review_status IN ({placeholders})")
                params.extend(values)
        else:
            where_clauses.append("s.review_status = ?")
            params.append(status)
    if source is not None:
        if isinstance(source, (list, tuple)):
            values = [s for s in source if s]
            if values:
                where_clauses.append(f"s.source IN ({','.join('?' for _ in values)})")
                params.extend(values)
        else:
            where_clauses.append("s.source = ?")
            params.append(source)
    if project is not None:
        where_clauses.append("s.project = ?")
        params.append(project)
    if task_type is not None:
        where_clauses.append("COALESCE(s.ai_task_type, s.task_type) = ?")
        params.append(task_type)
    if recovery_label is not None:
        where_clauses.append(
            "EXISTS (SELECT 1 FROM json_each(COALESCE(s.ai_recovery_labels, '[]')) WHERE value = ?)"
        )
        params.append(recovery_label)
    if failure_attribution is not None:
        where_clauses.append("s.ai_failure_attribution = ?")
        params.append(failure_attribution)
    if failure_mode is not None:
        where_clauses.append(
            "EXISTS (SELECT 1 FROM json_each(COALESCE(s.ai_failure_modes, '[]')) WHERE value = ?)"
        )
        params.append(failure_mode)
    if exclude_segmented_parents:
        where_clauses.append("s.review_status != 'segmented'")

    sql = base
    for clause in where_clauses:
        sql += f" AND {clause}"
    # Productivity sort should mean "recent 5-star first, then lower
    # scores, then unscored at the bottom" — a lone ``ORDER BY
    # ai_quality_score DESC`` puts NULLs at the TOP in SQLite (NULL >
    # any value when descending) AND shuffles old 5-star sessions over
    # new ones arbitrarily. The composite tiebreak fixes both:
    # non-NULL first, then score DESC, then newest within each score.
    if sort == "ai_failure_value_score":
        sql += (
            " ORDER BY (s.ai_failure_value_score IS NULL), "
            f"s.ai_failure_value_score {order.upper()}, "
            "s.start_time DESC LIMIT ? OFFSET ?"
        )
    elif sort == "ai_quality_score":
        sql += (
            " ORDER BY (s.ai_quality_score IS NULL), "
            f"s.ai_quality_score {order.upper()}, "
            "s.start_time DESC LIMIT ? OFFSET ?"
        )
    else:
        sql += f" ORDER BY s.{sort} {order.upper()} LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def get_session_detail(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    """Return full session detail including messages loaded from blob.

    Returns None if the session is not found.
    """
    row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None

    result = dict(row)

    # Load messages from blob
    blob_data = _read_blob_for_revision(session_id, result.get("blob_path"))
    result["messages"] = blob_data.get("messages", []) if blob_data else []

    # Parse JSON fields
    for field in (
        "value_badges", "risk_badges", "files_touched", "commands_run",
        "ai_recovery_labels", "ai_failure_modes",
    ):
        val = result.get(field)
        if isinstance(val, str):
            try:
                result[field] = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                pass

    return result


def update_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    status: str | None = None,
    notes: str | None = None,
    reason: str | None = None,
    ai_quality_score: int | None = None,
    ai_score_reason: str | None = None,
    ai_effort_estimate: float | None = None,
    ai_summary: str | None = None,
    ai_scoring_detail: str | None = None,
    ai_task_type: str | None = None,
    ai_outcome_badge: str | None = None,
    ai_value_badges: str | None = None,
    ai_risk_badges: str | None = None,
    ai_display_title: str | None = None,
    ai_failure_value_score: int | None = None,
    ai_recovery_labels: str | None = None,
    ai_failure_attribution: str | None = None,
    ai_failure_modes: str | None = None,
    ai_learning_summary: str | None = None,
    ai_scorer_backend: str | None = None,
    ai_scorer_model: str | None = None,
    ai_rubric_git_sha: str | None = None,
    ai_scored_at: str | None = None,
) -> bool:
    """Update review fields on a session.

    Sets reviewed_at when status changes. Returns True if the session was
    found and updated, False otherwise.
    """
    if ai_quality_score is not None:
        ai_quality_score = int(ai_quality_score)
        if not (1 <= ai_quality_score <= 5):
            return False
    if ai_failure_value_score is not None:
        ai_failure_value_score = int(ai_failure_value_score)
        if not (1 <= ai_failure_value_score <= 5):
            return False

    row = conn.execute(
        "SELECT session_id, review_status FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return False

    updates: list[str] = []
    params: list[Any] = []
    now = _now_iso()

    if status is not None:
        updates.append("review_status = ?")
        params.append(status)
        if status != row["review_status"]:
            updates.append("reviewed_at = ?")
            params.append(now)

    if notes is not None:
        updates.append("reviewer_notes = ?")
        params.append(notes)

    if reason is not None:
        updates.append("selection_reason = ?")
        params.append(reason)

    if ai_quality_score is not None:
        updates.append("ai_quality_score = ?")
        params.append(ai_quality_score)

    if ai_score_reason is not None:
        updates.append("ai_score_reason = ?")
        params.append(ai_score_reason)

    if ai_effort_estimate is not None:
        updates.append("ai_effort_estimate = ?")
        params.append(ai_effort_estimate)

    if ai_summary is not None:
        updates.append("ai_summary = ?")
        params.append(ai_summary)

    if ai_scoring_detail is not None:
        updates.append("ai_scoring_detail = ?")
        params.append(ai_scoring_detail)

    if ai_task_type is not None:
        updates.append("ai_task_type = ?")
        params.append(ai_task_type)

    if ai_outcome_badge is not None:
        updates.append("ai_outcome_badge = ?")
        params.append(ai_outcome_badge)

    if ai_value_badges is not None:
        updates.append("ai_value_badges = ?")
        params.append(ai_value_badges)

    if ai_risk_badges is not None:
        updates.append("ai_risk_badges = ?")
        params.append(ai_risk_badges)

    if ai_display_title is not None:
        updates.append("ai_display_title = ?")
        params.append(ai_display_title)

    if ai_failure_value_score is not None:
        updates.append("ai_failure_value_score = ?")
        params.append(ai_failure_value_score)

    if ai_recovery_labels is not None:
        updates.append("ai_recovery_labels = ?")
        params.append(ai_recovery_labels)

    if ai_failure_attribution is not None:
        updates.append("ai_failure_attribution = ?")
        params.append(ai_failure_attribution)

    if ai_failure_modes is not None:
        updates.append("ai_failure_modes = ?")
        params.append(ai_failure_modes)

    if ai_learning_summary is not None:
        updates.append("ai_learning_summary = ?")
        params.append(ai_learning_summary)

    if ai_scorer_backend is not None:
        updates.append("ai_scorer_backend = ?")
        params.append(ai_scorer_backend)

    if ai_scorer_model is not None:
        updates.append("ai_scorer_model = ?")
        params.append(ai_scorer_model)

    if ai_rubric_git_sha is not None:
        updates.append("ai_rubric_git_sha = ?")
        params.append(ai_rubric_git_sha)

    if ai_scored_at is not None:
        updates.append("ai_scored_at = ?")
        params.append(ai_scored_at)

    if not updates:
        return True

    updates.append("updated_at = ?")
    params.append(now)
    params.append(session_id)

    conn.execute(
        f"UPDATE sessions SET {', '.join(updates)} WHERE session_id = ?",
        params,
    )
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# Hold-state lifecycle
# ---------------------------------------------------------------------------

HOLD_STATES = frozenset({"auto_redacted", "pending_review", "released", "embargoed"})


def set_hold_state(
    conn: sqlite3.Connection,
    session_id: str,
    to_state: str,
    *,
    changed_by: str,
    reason: str | None = None,
    embargo_until: str | None = None,
) -> bool:
    """Transition a session's hold_state, appending a history row.

    Validates the target state and its required fields (`embargoed`
    requires `embargo_until` in the future). `sessions.hold_state` and
    the `session_hold_history` insert happen inside one transaction
    so the denormalized cache and the audit log never disagree.

    Returns True on success, False if the session is missing. Invalid
    state transitions raise `ValueError`.
    """
    if to_state not in HOLD_STATES:
        raise ValueError(f"invalid hold_state: {to_state!r}")
    if to_state == "embargoed":
        if not embargo_until:
            raise ValueError("embargoed requires embargo_until (ISO 8601)")
        try:
            parsed = datetime.fromisoformat(embargo_until)
        except ValueError as exc:
            raise ValueError(f"embargo_until is not ISO 8601: {embargo_until!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed <= datetime.now(timezone.utc):
            raise ValueError("embargo_until must be in the future; use release instead")
    else:
        embargo_until = None

    row = conn.execute(
        "SELECT hold_state, embargo_until FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return False

    now = _now_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE sessions SET hold_state = ?, embargo_until = ?, updated_at = ? "
            "WHERE session_id = ?",
            (to_state, embargo_until, now, session_id),
        )
        conn.execute(
            "INSERT INTO session_hold_history "
            "(history_id, session_id, from_state, to_state, embargo_until, "
            " changed_by, changed_at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), session_id, row["hold_state"], to_state,
             embargo_until, changed_by, now, reason),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def get_hold_history(
    conn: sqlite3.Connection, session_id: str,
) -> list[dict[str, Any]]:
    """Return the full hold-state timeline for a session, oldest first."""
    rows = conn.execute(
        "SELECT history_id, session_id, from_state, to_state, embargo_until, "
        "       changed_by, changed_at, reason "
        "FROM session_hold_history WHERE session_id = ? "
        "ORDER BY changed_at ASC",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


SHAREABLE_HOLD_STATES = frozenset({"auto_redacted", "released"})


def release_gate_blockers(
    conn: sqlite3.Connection, session_ids: list[str],
    *, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return sessions whose effective hold_state blocks hosted upload.

    Default-shareable: `auto_redacted` and `released` pass. Only explicit
    holds (`pending_review`, active `embargoed`) block. Auto-expired
    embargoes pass through via `effective_hold_state`. Returns `[]` when
    every session clears. Callers surface the result as a share-time error.
    """
    if not session_ids:
        return []
    placeholders = ",".join("?" * len(session_ids))
    rows = conn.execute(
        f"SELECT session_id, hold_state, embargo_until FROM sessions "
        f"WHERE session_id IN ({placeholders})",
        session_ids,
    ).fetchall()
    seen = {r["session_id"]: r for r in rows}
    blockers: list[dict[str, Any]] = []
    for sid in session_ids:
        row = seen.get(sid)
        if row is None:
            blockers.append({"session_id": sid, "hold_state": "missing"})
            continue
        effective = effective_hold_state(row["hold_state"], row["embargo_until"], now=now)
        if effective not in SHAREABLE_HOLD_STATES:
            blockers.append({
                "session_id": sid,
                "hold_state": effective,
                "embargo_until": row["embargo_until"],
            })
    return blockers


def build_session_redactions_summary(
    conn: sqlite3.Connection, session_id: str,
) -> dict[str, Any]:
    """Aggregate findings counts per engine/rule/type for the share manifest.

    Produces the `redactions` block defined in docs/security-refactor.md
    §Bundle manifest provenance — aggregated counts only, no hashes,
    plaintext, or offsets. `applied` covers rows the share-time apply
    shim will redact (`open` or `accepted`); `ignored` covers rows
    skipped (`decided_by='allowlist'` gets the explicit `via: allowlist`
    tag so downstream reviewers can tell user-authored skips apart).
    """
    rev_row = conn.execute(
        "SELECT findings_revision FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    findings_revision = rev_row["findings_revision"] if rev_row else None

    rows = conn.execute(
        "SELECT engine, rule, entity_type, status, decided_by, COUNT(*) AS n "
        "FROM findings WHERE session_id = ? "
        "GROUP BY engine, rule, entity_type, status, decided_by",
        (session_id,),
    ).fetchall()

    applied: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for row in rows:
        entry = {
            "engine": row["engine"],
            "rule": row["rule"],
            "entity_type": row["entity_type"],
            "count": row["n"],
        }
        if row["status"] == "ignored":
            if row["decided_by"] == "allowlist":
                entry["via"] = "allowlist"
            else:
                entry["via"] = "user"
            ignored.append(entry)
        else:
            applied.append(entry)

    return {
        "findings_revision": findings_revision,
        "applied": applied,
        "ignored": ignored,
    }


def effective_hold_state(
    hold_state: str | None, embargo_until: str | None,
    *, now: datetime | None = None,
) -> str:
    """Return the operational hold_state, accounting for embargo expiry.

    An embargoed session whose `embargo_until <= now` is treated as
    `released` at share time without any DB mutation (Decision 3).
    Callers that gate on the effective state (share/upload) should use
    this; UI/audit surfaces read the raw column directly.
    """
    state = hold_state or "auto_redacted"
    if state != "embargoed" or not embargo_until:
        return state
    try:
        parsed = datetime.fromisoformat(embargo_until)
    except ValueError:
        return state
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return "released" if parsed <= current else state


# Seconds ``end_time`` must advance past ``ai_scored_at`` before an
# already-scored session counts as "grew after it was scored" and is
# re-selected. Small margin avoids re-scoring on a near-simultaneous
# score/finish.
RESCORE_GROWTH_MARGIN_SECONDS = 60

# Don't auto-score a session until it has been quiet this long. Prevents
# grading a still-running trace mid-flight, which can otherwise leave a stale
# early score behind.
SCORE_SETTLE_SECONDS = 180

_UNSCORED_RETURN_KEYS = (
    "session_id", "display_title", "task_type", "outcome_badge", "project", "source",
)


def _parse_score_ts(ts: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (accepting a trailing ``Z``) to aware UTC."""
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def query_unscored_sessions(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    source: str | list[str] | tuple[str, ...] | None = None,
    since: str | None = None,
    include_stale_scored: bool = False,
    settle_seconds: int = 0,
    growth_margin_seconds: int = RESCORE_GROWTH_MARGIN_SECONDS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return sessions that need (re)scoring.

    By default, returns sessions missing either legacy quality or failure-value
    score — a list of dicts with session_id, display_title, task_type,
    outcome_badge, project, and source. Segmented parent sessions
    (``review_status='segmented'``) are skipped — their scorable content lives
    in the per-segment child rows, not the parent umbrella.

    Two opt-in behaviors support the background scanner, which would otherwise
    grade a session once (often mid-flight) and never revisit it:

    - ``include_stale_scored``: also return *already-scored* sessions whose
      ``end_time`` advanced more than ``growth_margin_seconds`` past
      ``ai_scored_at`` — i.e. they kept going after they were graded. Re-scoring
      reads the current blob, so the stale early grade is corrected; once a
      session is scored after it finishes, ``ai_scored_at >= end_time`` and it
      drops out of this selection.
    - ``settle_seconds``: skip sessions whose last activity (``end_time``) is
      within ``settle_seconds`` of ``now`` — they are likely still in-flight, so
      scoring now would just produce another premature grade (and, on the
      re-score path, churn every scan cycle).

    With both opt-ins off (the default) the behavior and return shape are
    unchanged from the original contract.
    """
    extended = include_stale_scored or settle_seconds > 0
    params: list[Any] = []
    score_missing = "(ai_quality_score IS NULL OR ai_failure_value_score IS NULL)"
    if include_stale_scored:
        # Coarse pre-filter; the precise margin check happens in Python because
        # ``end_time`` (``…Z``) and ``ai_scored_at`` (``…+00:00``) use different
        # UTC spellings. Lexicographic ``>`` is safe as a *pre*-filter since a
        # truly grown session differs in the minute/second portion.
        where = (
            f"({score_missing} OR (ai_scored_at IS NOT NULL "
            "AND end_time IS NOT NULL AND end_time > ai_scored_at))"
        )
    else:
        where = score_missing
    sql = (
        "SELECT session_id, display_title, task_type, outcome_badge, project, source, "
        "end_time, ai_scored_at, ai_quality_score, ai_failure_value_score "
        f"FROM sessions WHERE {where} AND review_status != 'segmented'"
    )
    if since is not None:
        sql += " AND start_time >= ?"
        params.append(since)
    if source is not None:
        if isinstance(source, (list, tuple)):
            values = [s for s in source if s]
            if values:
                sql += f" AND source IN ({','.join('?' for _ in values)})"
                params.extend(values)
        else:
            sql += " AND source = ?"
            params.append(source)
    sql += " ORDER BY start_time DESC LIMIT ?"
    # Over-fetch when we will refine in Python so we can still return `limit`
    # rows after dropping non-stale / unsettled candidates.
    params.append(max(limit * 5, 200) if extended else limit)

    rows = conn.execute(sql, params).fetchall()
    if not extended:
        return [{k: row[k] for k in _UNSCORED_RETURN_KEYS} for row in rows]

    clock = now or datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for row in rows:
        never_scored = row["ai_quality_score"] is None or row["ai_failure_value_score"] is None
        end_dt = _parse_score_ts(row["end_time"])
        scored_dt = _parse_score_ts(row["ai_scored_at"])
        grew = (
            include_stale_scored
            and scored_dt is not None
            and end_dt is not None
            and (end_dt - scored_dt).total_seconds() > growth_margin_seconds
        )
        if not (never_scored or grew):
            continue
        if (
            settle_seconds
            and end_dt is not None
            and (clock - end_dt).total_seconds() < settle_seconds
        ):
            continue
        out.append({k: row[k] for k in _UNSCORED_RETURN_KEYS})
        if len(out) >= limit:
            break
    return out


def query_sessions_for_rescore(
    conn: sqlite3.Connection,
    *,
    window_days: int = 7,
    source: str | list[str] | tuple[str, ...] | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return sessions within a recent time window for rescoring.

    Unlike :func:`query_unscored_sessions`, this returns sessions
    *regardless* of whether they have an ``ai_failure_value_score`` —
    the caller is explicitly rebuilding scores within a bounded window
    (e.g. after a rubric change). Filters by ``start_time`` (rolling
    window from ``now()``) and an optional source scope. Segmented
    parent sessions are skipped — the scorable content lives in their
    per-segment child rows.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    params: list[Any] = [cutoff]
    sql = (
        "SELECT session_id, display_title, task_type, outcome_badge, project, source "
        "FROM sessions WHERE start_time >= ? AND review_status != 'segmented'"
    )
    if source is not None:
        if isinstance(source, (list, tuple)):
            values = [s for s in source if s]
            if values:
                sql += f" AND source IN ({','.join('?' for _ in values)})"
                params.extend(values)
        else:
            sql += " AND source = ?"
            params.append(source)
    sql += " ORDER BY start_time DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def search_fts(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Full-text search across session transcripts, titles, files, and commands.

    Returns session metadata ranked by FTS5 relevance (bm25).
    Returns an empty list if FTS is not available.
    """
    if not _has_fts(conn):
        return []

    terms = re.findall(r"\w+", query, flags=re.UNICODE)
    if not terms:
        return []
    normalized_query = " AND ".join(f'"{term}"' for term in terms)

    rows = conn.execute(
        "SELECT s.* FROM sessions s "
        "JOIN sessions_fts f ON s.session_id = f.session_id "
        "WHERE sessions_fts MATCH ? "
        "ORDER BY rank "
        "LIMIT ? OFFSET ?",
        (normalized_query, limit, offset),
    ).fetchall()
    return [dict(row) for row in rows]


def get_stats(
    conn: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Return aggregate counts grouped by status, source, and project."""
    result: dict[str, Any] = {"total": 0, "by_status": {}, "by_source": {}, "by_project": {}, "by_task_type": {}}
    where, params = _build_start_time_where(start=start, end=end)

    # Total
    row = conn.execute(f"SELECT COUNT(*) AS cnt FROM sessions{where}", params).fetchone()
    result["total"] = row["cnt"] if row else 0

    # By status
    for row in conn.execute(
        f"SELECT review_status, COUNT(*) AS cnt FROM sessions{where} GROUP BY review_status",
        params,
    ).fetchall():
        result["by_status"][row["review_status"]] = row["cnt"]

    # By source
    for row in conn.execute(
        f"SELECT source, COUNT(*) AS cnt FROM sessions{where} GROUP BY source",
        params,
    ).fetchall():
        result["by_source"][row["source"]] = row["cnt"]

    # By project
    for row in conn.execute(
        f"SELECT project, COUNT(*) AS cnt FROM sessions{where} GROUP BY project",
        params,
    ).fetchall():
        result["by_project"][row["project"]] = row["cnt"]

    # By task_type (prefer LLM classification when available)
    tt_where, tt_params = _build_start_time_where(
        start=start, end=end,
        base_clauses=["COALESCE(ai_task_type, task_type) IS NOT NULL"],
    )
    for row in conn.execute(
        "SELECT COALESCE(ai_task_type, task_type) AS tt, COUNT(*) AS cnt "
        f"FROM sessions{tt_where} "
        "GROUP BY tt ORDER BY cnt DESC",
        tt_params,
    ).fetchall():
        result["by_task_type"][row["tt"]] = row["cnt"]

    return result


def get_dashboard_analytics(
    conn: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Return dashboard analytics for the workbench UI."""
    result: dict[str, Any] = {}
    filtered_where, filtered_params = _build_start_time_where(start=start, end=end)
    dated_where, dated_params = _build_start_time_where(
        start=start,
        end=end,
        base_clauses=["start_time IS NOT NULL"],
    )

    # Summary. `total_tokens` sums all input buckets (including cached reads
    # and cache creation) plus output, matching how billing reports it.
    # Omitting cache_* under-counts Claude by ~50x.
    row = conn.execute(
        "SELECT COUNT(*) as total_sessions, "
        "SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) "
        "  + COALESCE(cache_read_tokens, 0) + COALESCE(cache_creation_tokens, 0)) as total_tokens, "
        "COUNT(DISTINCT project) as unique_projects, "
        "COUNT(DISTINCT source) as unique_sources, "
        "SUM(estimated_cost_usd) as total_cost, "
        "SUM(CASE WHEN estimated_cost_usd IS NOT NULL THEN 1 ELSE 0 END) as priced_sessions "
        f"FROM sessions{filtered_where}",
        filtered_params,
    ).fetchone()
    # Sessions whose model isn't in the pricing table have a NULL cost: they fall
    # out of SUM(total_cost) but stay in COUNT(*), so total_cost understates spend.
    # Expose the priced/unpriced split so the UI can disclose "N unpriced".
    total_sessions = row["total_sessions"] or 0
    priced_sessions = row["priced_sessions"] or 0
    result["summary"] = {
        "total_sessions": total_sessions,
        "total_tokens": row["total_tokens"] or 0,
        "unique_projects": row["unique_projects"] or 0,
        "unique_sources": row["unique_sources"] or 0,
        "total_cost": round(row["total_cost"] or 0, 2),
        "priced_sessions": priced_sessions,
        "unpriced_sessions": total_sessions - priced_sessions,
    }

    # Resolve rate with previous-period comparison for trend coloring
    def _compute_resolve_rate(rr_start: str | None, rr_end: str | None) -> float | None:
        # Denominator excludes sessions that normalize to `unscored` (no
        # label at all). Numerator is the normalized `resolved` bucket —
        # AI `resolved` plus heuristic `tests_passed`. The previous
        # formula folded heuristic `completed` into "resolved" too, but
        # `completed` is now `inconclusive` (no-signal fallback), which
        # would overstate success.
        rr_where, rr_params = _build_start_time_where(
            start=rr_start, end=rr_end,
            base_clauses=[f"({_OUTCOME_NORMALIZE_SQL}) != 'unscored'"],
        )
        r = conn.execute(
            "SELECT COUNT(*) as total, "
            f"SUM(CASE WHEN ({_OUTCOME_NORMALIZE_SQL}) = 'resolved' THEN 1 ELSE 0 END) as resolved "
            f"FROM sessions {rr_where}",
            rr_params,
        ).fetchone()
        total = r["total"] or 0
        return round((r["resolved"] or 0) / total, 3) if total > 0 else None

    result["resolve_rate"] = _compute_resolve_rate(start, end)

    # Previous period: shift start/end back by the same duration
    if start and end:
        from datetime import datetime as _dt, timedelta as _td
        try:
            s = _dt.fromisoformat(start)
            e = _dt.fromisoformat(end)
            delta = (e - s).days + 1
            prev_end = (s - _td(days=1)).strftime("%Y-%m-%d")
            prev_start = (s - _td(days=delta)).strftime("%Y-%m-%d")
            result["resolve_rate_previous"] = _compute_resolve_rate(prev_start, prev_end)
        except (ValueError, TypeError):
            result["resolve_rate_previous"] = None
    else:
        result["resolve_rate_previous"] = None

    # Read:Edit ratio from tool_counts
    re_row = conn.execute(
        "SELECT "
        "SUM(COALESCE(json_extract(tool_counts, '$.Read'), 0) + "
        "    COALESCE(json_extract(tool_counts, '$.Grep'), 0) + "
        "    COALESCE(json_extract(tool_counts, '$.Glob'), 0)) as reads, "
        "SUM(COALESCE(json_extract(tool_counts, '$.Edit'), 0) + "
        "    COALESCE(json_extract(tool_counts, '$.Write'), 0)) as edits "
        f"FROM sessions{filtered_where}",
        filtered_params,
    ).fetchone()
    reads = re_row["reads"] or 0
    edits = re_row["edits"] or 0
    result["read_edit_ratio"] = round(reads / max(edits, 1), 1) if (reads + edits) > 0 else None

    # Top tools aggregate from tool_counts
    tools_where, tools_params = _build_start_time_where(
        start=start, end=end,
        base_clauses=["tool_counts IS NOT NULL", "tool_counts != '{}'"],
    )
    tool_rows = conn.execute(
        "SELECT key as tool, SUM(value) as calls "
        "FROM sessions, json_each(tool_counts) "
        f"{tools_where} "
        "GROUP BY key ORDER BY calls DESC LIMIT 10",
        tools_params,
    ).fetchall()
    result["top_tools"] = [dict(r) for r in tool_rows]

    # Average user interrupts across sessions that had at least one
    int_where, int_params = _build_start_time_where(
        start=start, end=end,
        base_clauses=["user_interrupts > 0"],
    )
    int_row = conn.execute(
        "SELECT AVG(CAST(user_interrupts AS REAL)) as avg_interrupts "
        f"FROM sessions{int_where}",
        int_params,
    ).fetchone()
    avg_int = int_row["avg_interrupts"]
    result["avg_interrupts"] = round(avg_int, 2) if avg_int is not None else None

    # Activity per day (last 30 days)
    rows = conn.execute(
        "SELECT DATE(start_time) as day, COUNT(*) as count FROM sessions "
        f"{dated_where} GROUP BY DATE(start_time) "
        "ORDER BY day DESC LIMIT 30",
        dated_params,
    ).fetchall()
    result["activity"] = [dict(r) for r in rows]

    # Outcome distribution — normalized to the clean dashboard vocabulary
    # so we don't show AI `resolved` next to heuristic `tests_passed`
    # (same underlying fact) or collide AI `partial` (progress made)
    # with heuristic `partial` (user interrupted the session).
    outcome_where, outcome_params = _build_start_time_where(
        start=start, end=end,
        base_clauses=[f"({_OUTCOME_NORMALIZE_SQL}) != 'unscored'"],
    )
    rows = conn.execute(
        f"SELECT ({_OUTCOME_NORMALIZE_SQL}) as outcome_label, "
        f"COUNT(*) as count FROM sessions {outcome_where} "
        "GROUP BY 1",
        outcome_params,
    ).fetchall()
    result["by_outcome_label"] = [dict(r) for r in rows]

    # Value badge distribution (prefer LLM classification)
    rows = conn.execute(
        "SELECT j.value as badge, COUNT(*) as count "
        "FROM sessions, json_each(COALESCE(ai_value_badges, value_badges)) j "
        f"{filtered_where} GROUP BY j.value",
        filtered_params,
    ).fetchall()
    result["by_value_label"] = [dict(r) for r in rows]

    # Risk badge distribution (prefer LLM classification)
    rows = conn.execute(
        "SELECT j.value as badge, COUNT(*) as count "
        "FROM sessions, json_each(COALESCE(sessions.ai_risk_badges, sessions.risk_badges)) j "
        f"{filtered_where} GROUP BY j.value",
        filtered_params,
    ).fetchall()
    result["by_risk_level"] = [dict(r) for r in rows]

    # Task type (prefer LLM classification)
    task_where, task_params = _build_start_time_where(
        start=start, end=end,
        base_clauses=["COALESCE(ai_task_type, task_type) IS NOT NULL"],
    )
    # NOTE: Must GROUP BY 1 (ordinal), not by the alias. SQLite resolves
    # `GROUP BY task_type` to the raw `task_type` column, not the
    # COALESCE expression, because the alias name collides with a real
    # column — producing multiple rows for the same displayed label
    # (e.g. two "unknown" rows when ai_task_type='unknown' differs from
    # the raw task_type).
    rows = conn.execute(
        "SELECT COALESCE(ai_task_type, task_type) as task_type, "
        f"COUNT(*) as count FROM sessions {task_where} "
        "GROUP BY 1 ORDER BY count DESC",
        task_params,
    ).fetchall()
    result["by_task_type"] = [dict(r) for r in rows]

    # Model (excludes parser-fallback `<synthetic>` sessions — see
    # clawjournal/scoring/insights.py:55 for rationale).
    model_where, model_params = _build_start_time_where(
        start=start, end=end,
        base_clauses=["model IS NOT NULL", "model != '<synthetic>'"],
    )
    # Split same-model traffic across effort tiers (e.g. "gpt-5.4 @ high"
    # vs "gpt-5.4 @ xhigh"); fall back to bare model when effort is NULL.
    rows = conn.execute(
        "SELECT CASE WHEN model_effort IS NOT NULL AND model_effort != '' "
        "       THEN model || ' @ ' || model_effort ELSE model END as model, "
        "COUNT(*) as count FROM sessions "
        f"{model_where} GROUP BY 1 ORDER BY count DESC",
        model_params,
    ).fetchall()
    result["by_model"] = [dict(r) for r in rows]

    # Tokens by source. `input_tokens` holds the fresh non-cached bucket
    # only; Claude's heavy prompt caching means the full input seen by
    # the model is input + cache_read + cache_creation. Sum all three
    # for the display bar so Claude's input doesn't look artificially
    # 50x smaller than output.
    rows = conn.execute(
        "SELECT source, "
        "SUM(input_tokens) + SUM(COALESCE(cache_read_tokens, 0)) + SUM(COALESCE(cache_creation_tokens, 0)) as input_tokens, "
        "SUM(output_tokens) as output_tokens "
        f"FROM sessions{filtered_where} GROUP BY source",
        filtered_params,
    ).fetchall()
    result["tokens_by_source"] = [dict(r) for r in rows]

    # Quality score distribution
    scored_where, scored_params = _build_start_time_where(
        start=start, end=end, base_clauses=["ai_quality_score IS NOT NULL"],
    )
    rows = conn.execute(
        "SELECT ai_quality_score as score, COUNT(*) as count FROM sessions "
        f"{scored_where} GROUP BY ai_quality_score ORDER BY ai_quality_score",
        scored_params,
    ).fetchall()
    result["by_quality_score"] = [dict(r) for r in rows]
    unscored_where, unscored_params = _build_start_time_where(
        start=start, end=end, base_clauses=["ai_quality_score IS NULL"],
    )
    result["unscored_count"] = conn.execute(
        f"SELECT COUNT(*) as cnt FROM sessions {unscored_where}",
        unscored_params,
    ).fetchone()["cnt"]

    failure_scored_where, failure_scored_params = _build_start_time_where(
        start=start, end=end, base_clauses=["ai_failure_value_score IS NOT NULL"],
    )
    rows = conn.execute(
        "SELECT ai_failure_value_score as score, COUNT(*) as count FROM sessions "
        f"{failure_scored_where} GROUP BY ai_failure_value_score ORDER BY ai_failure_value_score",
        failure_scored_params,
    ).fetchall()
    result["by_failure_value_score"] = [dict(r) for r in rows]
    result["high_value_failure_count"] = conn.execute(
        f"SELECT COUNT(*) as cnt FROM sessions {failure_scored_where} "
        "AND ai_failure_value_score >= 4",
        failure_scored_params,
    ).fetchone()["cnt"]

    rows = conn.execute(
        "SELECT ai_failure_attribution as attribution, COUNT(*) as count FROM sessions "
        f"{failure_scored_where} AND ai_failure_attribution IS NOT NULL "
        "AND ai_failure_attribution != '' GROUP BY ai_failure_attribution ORDER BY count DESC",
        failure_scored_params,
    ).fetchall()
    result["by_failure_attribution"] = [dict(r) for r in rows]

    rows = conn.execute(
        "SELECT json_each.value as recovery_label, COUNT(*) as count "
        f"FROM sessions, json_each(COALESCE(ai_recovery_labels, '[]')) {failure_scored_where} "
        "GROUP BY json_each.value ORDER BY count DESC",
        failure_scored_params,
    ).fetchall()
    result["by_recovery_label"] = [dict(r) for r in rows]

    rows = conn.execute(
        "SELECT json_each.value as failure_mode, COUNT(*) as count "
        f"FROM sessions, json_each(COALESCE(ai_failure_modes, '[]')) {failure_scored_where} "
        "GROUP BY json_each.value ORDER BY count DESC",
        failure_scored_params,
    ).fetchall()
    result["by_failure_mode"] = [dict(r) for r in rows]

    # By agent (derived from source + client_origin + runtime_channel)
    rows = conn.execute(
        "SELECT CASE "
        "  WHEN source = 'claude' AND (client_origin = 'desktop' OR runtime_channel = 'local-agent') THEN 'Claude Desktop' "
        "  WHEN source = 'claude' THEN 'Claude Code' "
        "  WHEN source = 'claude-science' THEN 'Claude Science' "
        "  WHEN source = 'codex' AND client_origin = 'desktop' THEN 'Codex Desktop' "
        "  WHEN source = 'codex' THEN 'Codex' "
        "  WHEN source = 'openclaw' THEN 'OpenClaw' "
        "  WHEN source = 'cursor' THEN 'Cursor' "
        "  WHEN source = 'copilot' THEN 'Copilot CLI' "
        "  WHEN source = 'aider' THEN 'Aider' "
        "  ELSE source "
        "END as agent, COUNT(*) as count "
        f"FROM sessions{filtered_where} GROUP BY agent ORDER BY count DESC",
        filtered_params,
    ).fetchall()
    result["by_agent"] = [dict(r) for r in rows]

    # Weekly activity (more compact than daily)
    rows = conn.execute(
        "SELECT strftime('%Y-W%W', start_time) as week, "
        "MIN(DATE(start_time)) as week_start, "
        "COUNT(*) as count FROM sessions "
        f"{dated_where} GROUP BY week ORDER BY week DESC LIMIT 12",
        dated_params,
    ).fetchall()
    result["weekly_activity"] = [dict(r) for r in rows]

    return result


def get_highlights(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
    top_n: int = 3,
    min_quality: int = 4,
    min_failure_value: int | None = None,
) -> dict[str, Any]:
    """Pick a small curated set of 'worth a look' sessions for the dashboard.

    Selection recipe:
    1. Candidates have `end_time` within the last `days`, are fully scored
       (`ai_failure_value_score IS NOT NULL`), and meet the failure-value threshold.
    2. Order by `ai_failure_value_score DESC, end_time DESC`.
    3. Diversify across `source` — prefer one from each distinct agent
       (claude / codex / openclaw / etc.) before taking a second from any.
    4. If fewer than `top_n` distinct sources have candidates, fill from the
       remaining sorted list.

    Each result carries enough metadata for the dashboard card plus a
    one-line rationale string ("FV 5/5 · 3 days ago") so the UI doesn't
    have to re-derive it.
    """
    threshold = min_failure_value if min_failure_value is not None else min_quality
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff.isoformat()

    rows = conn.execute(
        """
        SELECT session_id, project, source, model,
               start_time, end_time, duration_seconds,
               display_title, ai_display_title, ai_summary,
               ai_quality_score, ai_failure_value_score,
               ai_learning_summary, ai_effort_estimate,
               outcome_badge, ai_outcome_badge
        FROM sessions
        WHERE end_time IS NOT NULL
          AND end_time >= ?
          AND ai_failure_value_score IS NOT NULL
          AND ai_failure_value_score >= ?
        ORDER BY ai_failure_value_score DESC, end_time DESC, ai_quality_score DESC
        """,
        (cutoff_iso, threshold),
    ).fetchall()

    candidates = [dict(r) for r in rows]

    # Diversify across source: first pass picks one per distinct source in
    # the sorted order, second pass fills from remaining.
    picked: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    leftovers: list[dict[str, Any]] = []

    for c in candidates:
        if len(picked) >= top_n:
            break
        src = c.get("source") or ""
        if src not in seen_sources:
            picked.append(c)
            seen_sources.add(src)
        else:
            leftovers.append(c)

    for c in leftovers:
        if len(picked) >= top_n:
            break
        picked.append(c)

    now = datetime.now(timezone.utc)

    def _rationale(s: dict[str, Any]) -> str:
        score = s.get("ai_failure_value_score")
        end_time = s.get("end_time") or ""
        try:
            end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            delta = now - end_dt
            if delta.total_seconds() < 3600:
                when = "just now"
            elif delta.days == 0:
                hours = int(delta.total_seconds() // 3600)
                when = f"{hours}h ago"
            elif delta.days == 1:
                when = "yesterday"
            else:
                when = f"{delta.days} days ago"
        except (ValueError, AttributeError):
            when = "recently"
        score_label = f"FV {score}/5" if score else "failure-value scored"
        return f"{score_label} · {when}"

    def _truncate(text: str | None, limit: int = 200) -> str:
        if not text:
            return ""
        clean = " ".join(text.split())
        if len(clean) <= limit:
            return clean
        cut = clean[:limit].rsplit(" ", 1)[0]
        return cut + "…"

    highlights = []
    for s in picked:
        outcome = s.get("ai_outcome_badge") or s.get("outcome_badge") or None
        highlights.append({
            "session_id": s["session_id"],
            "title": s.get("display_title") or s["session_id"],
            "project": s.get("project"),
            "source": s.get("source"),
            "model": s.get("model"),
            "start_time": s.get("start_time"),
            "end_time": s.get("end_time"),
            "duration_seconds": s.get("duration_seconds"),
            "ai_quality_score": s.get("ai_quality_score"),
            "ai_failure_value_score": s.get("ai_failure_value_score"),
            "ai_effort_estimate": s.get("ai_effort_estimate"),
            "outcome": outcome,
            "summary_teaser": _truncate(s.get("ai_learning_summary") or s.get("ai_summary")),
            "rationale": _rationale(s),
        })

    return {
        "highlights": highlights,
        "window_days": days,
        "min_quality": min_quality,
        "min_failure_value": threshold,
        "candidate_count": len(candidates),
    }


def link_subagent_hierarchy(conn: sqlite3.Connection) -> int:
    """Detect and link parent-child session relationships.

    Runs as a post-scan step. Detects subagent spawns by:
    1. Tool calls named 'Agent' or 'Task' in session messages (Claude Code)
    2. Sessions with matching parent_session_id already set by the parser
    3. Time-window heuristic: sessions in the same project with overlapping
       time where one starts shortly after a tool call in another

    Returns the number of links created.
    """
    links_created = 0

    # Step 1: Link sessions that already have parent_session_id from parsing
    rows = conn.execute(
        "SELECT session_id, parent_session_id FROM sessions "
        "WHERE parent_session_id IS NOT NULL"
    ).fetchall()
    parent_children: dict[str, list[str]] = {}
    assigned_parent_by_child: dict[str, str] = {}
    for r in rows:
        parent_id = r["parent_session_id"]
        child_id = r["session_id"]
        parent_children.setdefault(parent_id, []).append(child_id)
        assigned_parent_by_child[child_id] = parent_id

    # Step 2: Detect Agent/Task tool calls in session blobs.
    # Query all sessions for candidate matching, but only read blobs for
    # sessions that haven't been linked yet (subagent_session_ids IS NULL)
    # to avoid re-reading all blobs on every scan cycle.
    all_sessions = conn.execute(
        "SELECT session_id, project, source, start_time, end_time, "
        "blob_path, subagent_session_ids "
        "FROM sessions WHERE start_time IS NOT NULL "
        "ORDER BY start_time"
    ).fetchall()

    # Build a lookup for quick matching
    by_project: dict[str, list[dict]] = {}
    for s in all_sessions:
        proj = s["project"]
        by_project.setdefault(proj, []).append(dict(s))

    for project_sessions in by_project.values():
        for sess in project_sessions:
            # Skip blob reading for sessions that already have linked children
            if sess.get("subagent_session_ids"):
                continue
            blob_path = sess.get("blob_path")
            if not blob_path:
                continue
            blob_file = Path(blob_path)
            if not blob_file.exists():
                continue

            try:
                with open(blob_file) as f:
                    blob = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            # Look for Agent/Task tool calls
            spawned_descriptions: list[str] = []
            for msg in blob.get("messages", []):
                tool = msg.get("tool") or ""
                if tool in ("Agent", "Task"):
                    inp = msg.get("input", {})
                    if isinstance(inp, dict):
                        spawned_descriptions.append(
                            str(inp.get("description", ""))[:200]
                        )
                # Also check Anthropic API format
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            if block.get("name") in ("Agent", "Task"):
                                inp = block.get("input", {})
                                if isinstance(inp, dict):
                                    spawned_descriptions.append(
                                        str(inp.get("description", ""))[:200]
                                    )

            if not spawned_descriptions:
                continue

            # Find child sessions that started during this session's window
            parent_start = sess.get("start_time", "")
            parent_end = sess.get("end_time", "")
            if not parent_start or not parent_end:
                continue

            child_ids: list[str] = []
            for candidate in project_sessions:
                candidate_id = candidate["session_id"]
                if candidate_id == sess["session_id"]:
                    continue
                c_start = candidate.get("start_time", "")
                # Child must start during or shortly after parent
                if not (parent_start <= c_start <= parent_end):
                    continue
                assigned_parent = assigned_parent_by_child.get(candidate_id)
                if assigned_parent and assigned_parent != sess["session_id"]:
                    continue
                child_ids.append(candidate_id)
                assigned_parent_by_child[candidate_id] = sess["session_id"]

            if child_ids:
                # Update parent with child IDs
                existing_children = parent_children.get(sess["session_id"], [])
                new_children = sorted(set(existing_children + child_ids))
                parent_children[sess["session_id"]] = new_children

    # Step 3: Write all links to the database
    now = _now_iso()
    for parent_id, child_ids in parent_children.items():
        conn.execute(
            "UPDATE sessions SET subagent_session_ids = ?, updated_at = ? "
            "WHERE session_id = ?",
            (json.dumps(child_ids), now, parent_id),
        )
        for child_id in child_ids:
            cursor = conn.execute(
                "UPDATE sessions SET parent_session_id = ?, updated_at = ? "
                "WHERE session_id = ? AND parent_session_id IS NULL",
                (parent_id, now, child_id),
            )
            links_created += cursor.rowcount

    conn.commit()
    return links_created


def get_insights(
    conn: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Return deep activity insights for the given time range.

    If start/end are not provided, defaults to the last 7 days.
    Returns heatmap, focus map, productivity patterns, trends, and effort data.
    """
    result: dict[str, Any] = {}
    params_base: list[Any] = []
    where = "WHERE start_time IS NOT NULL"

    if start:
        where += " AND DATE(start_time) >= ?"
        params_base.append(start)
    if end:
        where += " AND DATE(start_time) <= ?"
        params_base.append(end)

    # Heatmap: sessions bucketed by date and hour
    rows = conn.execute(
        f"SELECT DATE(start_time) as day, "
        f"CAST(strftime('%H', start_time) AS INTEGER) as hour, "
        f"COUNT(*) as sessions, "
        f"SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) "
        f"  + COALESCE(cache_read_tokens, 0) + COALESCE(cache_creation_tokens, 0)) as tokens, "
        f"COALESCE(SUM(estimated_cost_usd), 0) as cost "
        f"FROM sessions {where} "
        f"GROUP BY day, hour ORDER BY day, hour",
        params_base,
    ).fetchall()
    result["heatmap"] = [dict(r) for r in rows]

    # Focus: sessions by project with cost and task type breakdown
    rows = conn.execute(
        f"SELECT project, COUNT(*) as sessions, "
        f"SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) "
        f"  + COALESCE(cache_read_tokens, 0) + COALESCE(cache_creation_tokens, 0)) as tokens, "
        f"COALESCE(SUM(estimated_cost_usd), 0) as cost "
        f"FROM sessions {where} "
        f"GROUP BY project ORDER BY sessions DESC LIMIT 20",
        params_base,
    ).fetchall()
    focus: list[dict[str, Any]] = []
    for r in rows:
        proj = dict(r)
        # Task type breakdown per project
        tt_rows = conn.execute(
            f"SELECT COALESCE(ai_task_type, task_type) as task_type, COUNT(*) as count "
            f"FROM sessions {where} AND project = ? "
            f"AND COALESCE(ai_task_type, task_type) IS NOT NULL "
            f"GROUP BY 1",
            [*params_base, proj["project"]],
        ).fetchall()
        proj["task_types"] = {r2["task_type"]: r2["count"] for r2 in tt_rows}
        focus.append(proj)
    result["focus"] = focus

    # Failure value: duration vs score. Returns the normalized resolution
    # (`resolved` / `failed` / `partial` / etc.) so the frontend scatter
    # plot only has to color-code one vocabulary. Before normalization,
    # heuristic badges like `tests_failed` / `build_failed` / `errored`
    # fell into the color map's default gray "Other" bucket instead of
    # red failures.
    rows = conn.execute(
        f"SELECT session_id, duration_seconds, ai_quality_score, ai_failure_value_score, "
        f"({_OUTCOME_NORMALIZE_SQL}) as resolution, "
        f"estimated_cost_usd as cost "
        f"FROM sessions {where} "
        f"AND duration_seconds IS NOT NULL AND ai_failure_value_score IS NOT NULL "
        f"ORDER BY start_time DESC LIMIT 200",
        params_base,
    ).fetchall()
    result["duration_vs_score"] = [dict(r) for r in rows]

    # Model effectiveness. Exclude parser-fallback `<synthetic>` sessions —
    # same rationale as scoring/insights.py:55 and the cli.py:479 export
    # filter. These sessions have no real model/cost and pollute the table.
    rows = conn.execute(
        f"SELECT CASE WHEN model_effort IS NOT NULL AND model_effort != '' "
        f"       THEN model || ' @ ' || model_effort ELSE model END as model, "
        f"COUNT(*) as sessions, "
        f"AVG(ai_failure_value_score) as avg_failure_value_score, "
        f"SUM(CASE WHEN ({_OUTCOME_NORMALIZE_SQL}) = 'resolved' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) as resolve_rate, "
        f"AVG(estimated_cost_usd) as avg_cost, "
        f"SUM(estimated_cost_usd) as total_cost "
        f"FROM sessions {where} AND model IS NOT NULL AND model != '<synthetic>' "
        f"GROUP BY 1 ORDER BY sessions DESC",
        params_base,
    ).fetchall()
    result["model_effectiveness"] = [
        {**dict(r), "avg_failure_value_score": round(r["avg_failure_value_score"] or 0, 1), "resolve_rate": round(r["resolve_rate"] or 0, 2), "avg_cost": round(r["avg_cost"] or 0, 4), "total_cost": round(r["total_cost"] or 0, 2)}
        for r in rows
    ]

    # Tool usage. Uses `tool_counts` (JSON map of tool-name → call-count)
    # rather than `commands_run` (list of raw shell strings). The prior
    # query summed bash command lines like "ls /Users/..." and displayed
    # them as the top tool, which was nonsensical. Mirrors the dashboard
    # top_tools query.
    rows = conn.execute(
        f"SELECT key as tool, SUM(value) as calls "
        f"FROM sessions, json_each(tool_counts) "
        f"{where} AND tool_counts IS NOT NULL AND tool_counts != '{{}}' "
        f"GROUP BY key ORDER BY calls DESC LIMIT 20",
        params_base,
    ).fetchall()
    result["tool_usage"] = [dict(r) for r in rows]

    # Trends: daily aggregates
    rows = conn.execute(
        f"SELECT DATE(start_time) as day, "
        f"COUNT(*) as sessions, "
        f"AVG(estimated_cost_usd) as avg_cost, "
        f"AVG(duration_seconds) as avg_duration, "
        f"SUM(CASE WHEN ({_OUTCOME_NORMALIZE_SQL}) = 'resolved' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) as resolve_rate "
        f"FROM sessions {where} "
        f"GROUP BY day ORDER BY day",
        params_base,
    ).fetchall()
    result["trends"] = [
        {**dict(r), "avg_cost": round(r["avg_cost"] or 0, 4), "resolve_rate": round(r["resolve_rate"] or 0, 2)}
        for r in rows
    ]

    # Effort distribution
    rows = conn.execute(
        f"SELECT CASE "
        f"  WHEN ai_effort_estimate < 0.2 THEN '0.0-0.2' "
        f"  WHEN ai_effort_estimate < 0.4 THEN '0.2-0.4' "
        f"  WHEN ai_effort_estimate < 0.6 THEN '0.4-0.6' "
        f"  WHEN ai_effort_estimate < 0.8 THEN '0.6-0.8' "
        f"  ELSE '0.8-1.0' END as bucket, "
        f"COUNT(*) as count "
        f"FROM sessions {where} AND ai_effort_estimate IS NOT NULL "
        f"GROUP BY bucket ORDER BY bucket",
        params_base,
    ).fetchall()
    result["effort_distribution"] = [dict(r) for r in rows]

    # Cost breakdown (excludes `<synthetic>` — same rationale). Splits
    # same-model spend across effort tiers.
    rows = conn.execute(
        f"SELECT CASE WHEN model_effort IS NOT NULL AND model_effort != '' "
        f"       THEN model || ' @ ' || model_effort ELSE model END as model, "
        f"COALESCE(SUM(estimated_cost_usd), 0) as cost "
        f"FROM sessions {where} AND model IS NOT NULL AND model != '<synthetic>' "
        f"GROUP BY 1 ORDER BY cost DESC",
        params_base,
    ).fetchall()
    result["cost_by_model"] = [dict(r) for r in rows]

    rows = conn.execute(
        f"SELECT project, COALESCE(SUM(estimated_cost_usd), 0) as cost "
        f"FROM sessions {where} "
        f"GROUP BY project ORDER BY cost DESC LIMIT 10",
        params_base,
    ).fetchall()
    result["cost_by_project"] = [dict(r) for r in rows]

    return result


def _latest_successful_revision(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    exclude_share_id: str | None = None,
) -> str | None:
    """Return the most recently uploaded content revision for a trace."""
    where = [
        "ss.session_id = ?",
        "sh.shared_at IS NOT NULL",
        "ss.content_revision IS NOT NULL",
    ]
    params: list[Any] = [session_id]
    if exclude_share_id is not None:
        where.append("ss.share_id != ?")
        params.append(exclude_share_id)
    row = conn.execute(
        "SELECT ss.content_revision FROM share_sessions ss "
        "JOIN shares sh ON sh.share_id = ss.share_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY sh.shared_at DESC, sh.created_at DESC, ss.share_id DESC "
        "LIMIT 1",
        params,
    ).fetchone()
    return row["content_revision"] if row is not None else None


def create_share(
    conn: sqlite3.Connection,
    session_ids: list[str],
    attestation: str | None = None,
    note: str | None = None,
    source_filter: str | list[str] | tuple[str, ...] | None = None,
    expected_revisions: dict[str, str] | None = None,
) -> str:
    """Create a share linking the given sessions.

    Returns the new share_id. ``expected_revisions`` lets callers bind a UI
    selection to the exact revisions the user reviewed; a concurrent append
    raises ``ValueError`` before any share row is created.
    """
    share_id = str(uuid.uuid4())
    now = _now_iso()
    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        # Verify all sessions exist while holding the same write transaction
        # that records their immutable share snapshots.
        found_sessions: dict[str, str | None] = {}
        if session_ids:
            placeholders = ", ".join("?" for _ in session_ids)
            source_clause = ""
            params: list[Any] = list(session_ids)
            if source_filter is not None:
                if isinstance(source_filter, (list, tuple)):
                    values = [s for s in source_filter if s]
                    if values:
                        source_clause = (
                            f" AND source IN ({','.join('?' for _ in values)})"
                        )
                        params.extend(values)
                else:
                    source_clause = " AND source = ?"
                    params.append(source_filter)
            rows = conn.execute(
                "SELECT session_id, content_revision FROM sessions "
                f"WHERE session_id IN ({placeholders}){source_clause}",
                params,
            ).fetchall()
            found_sessions = {
                row["session_id"]: row["content_revision"]
                for row in rows
            }

        if expected_revisions is not None:
            expected_ids = set(expected_revisions)
            found_ids = set(found_sessions)
            requested_ids = set(session_ids)
            blockers = [
                {
                    "session_id": sid,
                    "expected_revision_hash": expected_revisions.get(sid),
                    "current_revision_hash": found_sessions.get(sid),
                }
                for sid in sorted(expected_ids | found_ids | requested_ids)
                if (
                    sid not in expected_ids
                    or sid not in requested_ids
                    or sid not in found_ids
                    or expected_revisions[sid] != found_sessions[sid]
                )
            ]
            if blockers:
                raise RevisionConflictError(blockers)

        conn.execute(
            """INSERT INTO shares (
                share_id, created_at, session_count, status,
                attestation, submission_note
            ) VALUES (?, ?, ?, 'draft', ?, ?)""",
            (share_id, now, len(found_sessions), attestation, note),
        )

        for sid, content_revision in found_sessions.items():
            replaces_revision = _latest_successful_revision(conn, sid)
            conn.execute(
                "INSERT OR IGNORE INTO share_sessions "
                "(share_id, session_id, added_at, content_revision, replaces_revision) "
                "VALUES (?, ?, ?, ?, ?)",
                (share_id, sid, now, content_revision, replaces_revision),
            )
            conn.execute(
                "UPDATE sessions SET share_id = ?, updated_at = ? "
                "WHERE session_id = ?",
                (share_id, now, sid),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return share_id


def share_revision_blockers(
    conn: sqlite3.Connection,
    share_id: str,
) -> list[dict[str, Any]]:
    """Return selected share snapshots whose local trace has since changed."""
    rows = conn.execute(
        "SELECT ss.session_id, ss.content_revision AS expected_revision_hash, "
        "s.content_revision AS current_revision_hash, s.review_status "
        "FROM share_sessions ss "
        "JOIN sessions s ON s.session_id = ss.session_id "
        "WHERE ss.share_id = ? "
        "AND ss.content_revision IS NOT s.content_revision "
        "ORDER BY ss.session_id",
        (share_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def source_scope_blockers(
    conn: sqlite3.Connection,
    session_ids: list[str],
    source_filter: str | list[str] | tuple[str, ...] | None,
) -> list[dict[str, Any]]:
    """Return selected sessions outside the confirmed share source scope."""
    if not session_ids or source_filter is None:
        return []
    placeholders = ", ".join("?" for _ in session_ids)
    params: list[Any] = list(session_ids)
    if isinstance(source_filter, (list, tuple)):
        values = [s for s in source_filter if s]
        if not values:
            return []
        source_clause = f"source NOT IN ({','.join('?' for _ in values)})"
        params.extend(values)
        allowed = ", ".join(values)
    else:
        source_clause = "source != ?"
        params.append(source_filter)
        allowed = str(source_filter)
    rows = conn.execute(
        "SELECT session_id, project, source, display_title "
        f"FROM sessions WHERE session_id IN ({placeholders}) AND {source_clause} "
        "ORDER BY source, project, session_id",
        params,
    ).fetchall()
    blockers = [dict(row) for row in rows]
    for blocker in blockers:
        blocker["allowed_sources"] = allowed
    return blockers


def get_shares(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """List all shares ordered by creation time (newest first)."""
    rows = conn.execute(
        "SELECT * FROM shares ORDER BY created_at DESC"
    ).fetchall()
    return [_with_legacy_bundle_alias(dict(row)) for row in rows]


_AUTO_UPLOAD_MODES = frozenset({"off", "enabled", "paused"})
_AUTO_UPLOAD_HEALTH_VALUES = frozenset({"ready", "action_required", "retrying"})
_AUTO_UPLOAD_LIST_FIELDS = {
    "enrolled_sources": "enrolled_sources_json",
    "enrolled_projects": "enrolled_projects_json",
    "hook_targets": "hook_targets_json",
}


def _canonical_auto_upload_strings(
    values: Iterable[str],
    *,
    field: str,
    allow_empty: bool = False,
) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field} must be a collection of strings")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must contain only non-empty strings")
        normalized.add(value.strip())
    if not normalized and not allow_empty:
        raise ValueError(f"{field} must not be empty")
    return sorted(normalized)


def _parse_auto_upload_timestamp(value: str, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be an ISO 8601 timestamp")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _decode_auto_upload_enrollment(row: sqlite3.Row) -> dict[str, Any]:
    enrollment = dict(row)
    for public_name, stored_name in _AUTO_UPLOAD_LIST_FIELDS.items():
        raw = enrollment.pop(stored_name)
        try:
            decoded = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"corrupt automatic-upload field: {stored_name}") from exc
        enrollment[public_name] = _canonical_auto_upload_strings(
            decoded,
            field=public_name,
            allow_empty=(public_name == "hook_targets"),
        )
    enrollment["revocation_pending"] = bool(enrollment["revocation_pending"])
    enrollment.pop("singleton_id", None)
    return enrollment


def get_auto_upload_enrollment(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Return the singleton local enrollment, decoding its exact scope."""
    row = conn.execute(
        "SELECT * FROM auto_upload_enrollment WHERE singleton_id = 1"
    ).fetchone()
    return _decode_auto_upload_enrollment(row) if row is not None else None


def save_auto_upload_enrollment(
    conn: sqlite3.Connection,
    *,
    mode: str,
    health: str,
    generation: int,
    enrolled_at: str,
    client_enrollment_id: str,
    enrolled_sources: Iterable[str],
    enrolled_projects: Iterable[str],
    server_enrollment_id: str | None = None,
    authorization_revision: int | None = None,
    recurring_authorization_version: str | None = None,
    retention_version: str | None = None,
    egress_profile_hash: str | None = None,
    hook_targets: Iterable[str] = (),
    claude_hook_observed_at: str | None = None,
    codex_hook_observed_at: str | None = None,
    last_completed_at: str | None = None,
    next_retry_at: str | None = None,
    consecutive_failures: int = 0,
    last_result_code: str | None = None,
    last_result_count: int | None = None,
    last_receipt_reference: str | None = None,
    current_run_id: str | None = None,
    current_run_stage: str | None = None,
    revocation_pending: bool = False,
) -> dict[str, Any]:
    """Atomically create or replace the one authoritative local enrollment."""
    if mode not in _AUTO_UPLOAD_MODES:
        raise ValueError(f"invalid automatic-upload mode: {mode!r}")
    if health not in _AUTO_UPLOAD_HEALTH_VALUES:
        raise ValueError(f"invalid automatic-upload health: {health!r}")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ValueError("generation must be a positive integer")
    _parse_auto_upload_timestamp(enrolled_at, field="enrolled_at")
    for field_name, timestamp in (
        ("last_completed_at", last_completed_at),
        ("next_retry_at", next_retry_at),
        ("claude_hook_observed_at", claude_hook_observed_at),
        ("codex_hook_observed_at", codex_hook_observed_at),
    ):
        if timestamp is not None:
            _parse_auto_upload_timestamp(timestamp, field=field_name)
    if not isinstance(client_enrollment_id, str) or not client_enrollment_id.strip():
        raise ValueError("client_enrollment_id must be a non-empty string")
    if (
        authorization_revision is not None
        and (
            not isinstance(authorization_revision, int)
            or isinstance(authorization_revision, bool)
            or authorization_revision < 1
        )
    ):
        raise ValueError("authorization_revision must be a positive integer")
    if (
        not isinstance(consecutive_failures, int)
        or isinstance(consecutive_failures, bool)
        or consecutive_failures < 0
    ):
        raise ValueError("consecutive_failures must be a non-negative integer")
    if (
        last_result_count is not None
        and (
            not isinstance(last_result_count, int)
            or isinstance(last_result_count, bool)
            or last_result_count < 0
        )
    ):
        raise ValueError("last_result_count must be a non-negative integer")

    payload: dict[str, Any] = {
        "mode": mode,
        "health": health,
        "generation": generation,
        "enrolled_at": enrolled_at,
        "client_enrollment_id": client_enrollment_id.strip(),
        "enrolled_sources_json": json.dumps(
            _canonical_auto_upload_strings(
                enrolled_sources, field="enrolled_sources"
            ),
            separators=(",", ":"),
        ),
        "enrolled_projects_json": json.dumps(
            _canonical_auto_upload_strings(
                enrolled_projects, field="enrolled_projects"
            ),
            separators=(",", ":"),
        ),
        "server_enrollment_id": server_enrollment_id,
        "authorization_revision": authorization_revision,
        "recurring_authorization_version": recurring_authorization_version,
        "retention_version": retention_version,
        "egress_profile_hash": egress_profile_hash,
        "hook_targets_json": json.dumps(
            _canonical_auto_upload_strings(
                hook_targets, field="hook_targets", allow_empty=True
            ),
            separators=(",", ":"),
        ),
        "claude_hook_observed_at": claude_hook_observed_at,
        "codex_hook_observed_at": codex_hook_observed_at,
        "last_completed_at": last_completed_at,
        "next_retry_at": next_retry_at,
        "consecutive_failures": consecutive_failures,
        "last_result_code": last_result_code,
        "last_result_count": last_result_count,
        "last_receipt_reference": last_receipt_reference,
        "current_run_id": current_run_id,
        "current_run_stage": current_run_stage,
        "revocation_pending": int(bool(revocation_pending)),
        "updated_at": _now_iso(),
    }
    columns = list(payload)
    placeholders = ", ".join("?" for _ in columns)
    assignments = ", ".join(f"{column} = excluded.{column}" for column in columns)
    conn.execute(
        "INSERT INTO auto_upload_enrollment "
        f"(singleton_id, {', '.join(columns)}) VALUES (1, {placeholders}) "
        f"ON CONFLICT(singleton_id) DO UPDATE SET {assignments}",
        [payload[column] for column in columns],
    )
    conn.commit()
    enrollment = get_auto_upload_enrollment(conn)
    assert enrollment is not None
    return enrollment


def update_auto_upload_enrollment(
    conn: sqlite3.Connection,
    *,
    expected_generation: int | None = None,
    **changes: Any,
) -> bool:
    """Update enrollment fields, optionally using generation as a CAS guard."""
    if not changes:
        return get_auto_upload_enrollment(conn) is not None

    stored_changes: dict[str, Any] = {}
    allowed = {
        "mode", "health", "generation", "enrolled_at",
        "client_enrollment_id", "server_enrollment_id",
        "authorization_revision", "recurring_authorization_version",
        "retention_version", "egress_profile_hash", "last_completed_at",
        "next_retry_at", "consecutive_failures", "last_result_code",
        "last_result_count", "last_receipt_reference", "current_run_id",
        "current_run_stage", "revocation_pending",
        "claude_hook_observed_at", "codex_hook_observed_at",
        *_AUTO_UPLOAD_LIST_FIELDS,
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unknown automatic-upload fields: {', '.join(sorted(unknown))}")

    for field, value in changes.items():
        if field in _AUTO_UPLOAD_LIST_FIELDS:
            values = _canonical_auto_upload_strings(
                value,
                field=field,
                allow_empty=(field == "hook_targets"),
            )
            stored_changes[_AUTO_UPLOAD_LIST_FIELDS[field]] = json.dumps(
                values, separators=(",", ":")
            )
        else:
            stored_changes[field] = value

    if "mode" in changes and changes["mode"] not in _AUTO_UPLOAD_MODES:
        raise ValueError(f"invalid automatic-upload mode: {changes['mode']!r}")
    if "health" in changes and changes["health"] not in _AUTO_UPLOAD_HEALTH_VALUES:
        raise ValueError(f"invalid automatic-upload health: {changes['health']!r}")
    for field in ("generation", "authorization_revision"):
        value = changes.get(field)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 1
        ):
            raise ValueError(f"{field} must be a positive integer")
    for field in ("consecutive_failures", "last_result_count"):
        value = changes.get(field)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ValueError(f"{field} must be a non-negative integer")
    for field in (
        "enrolled_at",
        "last_completed_at",
        "next_retry_at",
        "claude_hook_observed_at",
        "codex_hook_observed_at",
    ):
        value = changes.get(field)
        if value is not None:
            _parse_auto_upload_timestamp(value, field=field)
    if "client_enrollment_id" in changes and (
        not isinstance(changes["client_enrollment_id"], str)
        or not changes["client_enrollment_id"].strip()
    ):
        raise ValueError("client_enrollment_id must be a non-empty string")
    if "revocation_pending" in changes:
        stored_changes["revocation_pending"] = int(bool(changes["revocation_pending"]))

    stored_changes["updated_at"] = _now_iso()
    assignments = ", ".join(f"{field} = ?" for field in stored_changes)
    params = list(stored_changes.values())
    where = "singleton_id = 1"
    if expected_generation is not None:
        if (
            not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or expected_generation < 1
        ):
            raise ValueError("expected_generation must be a positive integer")
        where += " AND generation = ?"
        params.append(expected_generation)
    cursor = conn.execute(
        f"UPDATE auto_upload_enrollment SET {assignments} WHERE {where}",
        params,
    )
    conn.commit()
    return cursor.rowcount == 1


_LATEST_SUCCESSFUL_REVISIONS_CTE = """
WITH ranked_shared_revisions AS (
    SELECT
        ss.session_id,
        ss.content_revision,
        ROW_NUMBER() OVER (
            PARTITION BY ss.session_id
            ORDER BY sh.shared_at DESC, sh.created_at DESC, ss.share_id DESC
        ) AS revision_rank
    FROM share_sessions ss
    JOIN shares sh ON sh.share_id = ss.share_id
    WHERE sh.shared_at IS NOT NULL AND ss.content_revision IS NOT NULL
),
latest_shared_revisions AS (
    SELECT session_id, content_revision
    FROM ranked_shared_revisions
    WHERE revision_rank = 1
)
"""

_SUCCESSFUL_EXACT_REVISION_EXISTS_SQL = """
EXISTS (
    SELECT 1
    FROM share_sessions exact_ss
    JOIN shares exact_share ON exact_share.share_id = exact_ss.share_id
    WHERE exact_ss.session_id = s.session_id
      AND exact_ss.content_revision = s.content_revision
      AND exact_share.shared_at IS NOT NULL
)
"""


_AUTO_UPLOAD_EXCLUSION_REASONS = (
    "pre_enrollment",
    "unsupported_unsettled",
    "held_or_embargoed",
    "blocked_review_status",
    "changed_revision_needing_approval",
    "already_shared",
    "source_excluded",
    "project_excluded",
    "missing_blob",
    "raw_source_unavailable",
    "scope_confirmation_changed",
)
AUTO_UPLOAD_ALLOWED_REVIEW_STATUSES = frozenset({"new", "shortlisted", "approved"})


def _auto_upload_raw_source_resolvable(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    if path.is_file():
        return True
    return bool(
        path.is_dir()
        and (path / "subagents").is_dir()
        and any((path / "subagents").glob("agent-*.jsonl"))
    )


def get_auto_upload_candidate_report(
    conn: sqlite3.Connection,
    *,
    current_sources: Iterable[str],
    current_projects: Iterable[str],
    source_confirmed: bool,
    projects_confirmed: bool,
    completion_modes: Mapping[str, str],
    now: datetime | None = None,
    limit: int = AUTO_UPLOAD_CANDIDATE_LIMIT,
) -> dict[str, Any]:
    """Return the deterministic, safety-gated automatic-upload candidates.

    ``completion_modes`` is the audited per-source contract supplied by the
    parser layer: each enrolled source maps to ``explicit_close`` or
    ``stable_revision``. The latter requires both ``end_time`` and the current
    content revision to have remained unchanged for 24 hours. This helper only
    reads stored scores; it never invokes scoring or mutates review state.

    Newly discovered sources/projects remain excluded without changing the
    enrollment. Removing confirmation for an enrolled scope fails closed.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if not isinstance(source_confirmed, bool) or not isinstance(projects_confirmed, bool):
        raise ValueError("source_confirmed and projects_confirmed must be booleans")
    if not isinstance(completion_modes, Mapping):
        raise ValueError("completion_modes must map source names to completion modes")
    invalid_modes = {
        source: mode
        for source, mode in completion_modes.items()
        if mode not in {"explicit_close", "stable_revision"}
    }
    if invalid_modes:
        raise ValueError(f"invalid completion modes: {invalid_modes!r}")

    exclusion_counts = {reason: 0 for reason in _AUTO_UPLOAD_EXCLUSION_REASONS}
    base_report: dict[str, Any] = {
        "eligible": [],
        "selected": [],
        "eligible_count": 0,
        "selected_count": 0,
        "deferred_by_cap": 0,
        "exclusion_counts": exclusion_counts,
        "exclusions": [],
        "scope_blockers": [],
        "limit": limit,
    }
    enrollment = get_auto_upload_enrollment(conn)
    if enrollment is None:
        base_report["scope_blockers"] = ["not_enrolled"]
        return base_report

    enrolled_sources = set(enrollment["enrolled_sources"])
    enrolled_projects = set(enrollment["enrolled_projects"])
    active_sources = set(_canonical_auto_upload_strings(
        current_sources, field="current_sources", allow_empty=True
    ))
    active_projects = set(_canonical_auto_upload_strings(
        current_projects, field="current_projects", allow_empty=True
    ))
    scope_blockers: list[str] = []
    if not source_confirmed:
        scope_blockers.append("source_confirmation_missing")
    if not projects_confirmed:
        scope_blockers.append("project_confirmation_missing")
    if not enrolled_sources.issubset(active_sources):
        scope_blockers.append("enrolled_source_removed")
    if not enrolled_projects.issubset(active_projects):
        scope_blockers.append("enrolled_project_removed")
    base_report["scope_blockers"] = scope_blockers

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    else:
        current_time = current_time.astimezone(timezone.utc)
    enrolled_at = _parse_auto_upload_timestamp(
        enrollment["enrolled_at"], field="enrolled_at"
    )
    stable_cutoff = current_time - timedelta(hours=AUTO_UPLOAD_STABILITY_HOURS)

    rows = conn.execute(
        _LATEST_SUCCESSFUL_REVISIONS_CTE
        + "SELECT s.session_id, s.project, s.source, s.display_title, "
        "s.end_time, s.review_status, s.hold_state, s.embargo_until, "
        "s.blob_path, s.raw_source_path, s.content_revision AS revision_hash, "
        "s.revision_stable_since, s.ai_failure_value_score, "
        "latest.content_revision AS last_shared_revision_hash, "
        f"({_SUCCESSFUL_EXACT_REVISION_EXISTS_SQL}) "
        "AS already_shared_exact_revision "
        "FROM sessions s "
        "LEFT JOIN latest_shared_revisions latest ON latest.session_id = s.session_id "
        "ORDER BY s.session_id"
    ).fetchall()

    eligible: list[dict[str, Any]] = []
    exclusions: list[dict[str, str]] = []

    def exclude(row: sqlite3.Row, reason: str) -> None:
        exclusion_counts[reason] += 1
        exclusions.append({"session_id": row["session_id"], "reason": reason})

    for row in rows:
        if row["source"] not in enrolled_sources:
            exclude(row, "source_excluded")
            continue
        if row["project"] not in enrolled_projects:
            exclude(row, "project_excluded")
            continue
        if scope_blockers:
            exclude(row, "scope_confirmation_changed")
            continue

        try:
            end_time = _parse_auto_upload_timestamp(row["end_time"], field="end_time")
        except ValueError:
            exclude(row, "unsupported_unsettled")
            continue
        if end_time <= enrolled_at:
            exclude(row, "pre_enrollment")
            continue
        if end_time > current_time:
            exclude(row, "unsupported_unsettled")
            continue

        completion_mode = completion_modes.get(row["source"])
        if completion_mode == "stable_revision":
            try:
                stable_since = _parse_auto_upload_timestamp(
                    row["revision_stable_since"], field="revision_stable_since"
                )
            except ValueError:
                exclude(row, "unsupported_unsettled")
                continue
            if stable_since > stable_cutoff or end_time > stable_cutoff:
                exclude(row, "unsupported_unsettled")
                continue
        elif completion_mode != "explicit_close":
            exclude(row, "unsupported_unsettled")
            continue

        latest_revision = row["last_shared_revision_hash"]
        current_revision = row["revision_hash"]
        if row["already_shared_exact_revision"]:
            exclude(row, "already_shared")
            continue
        if (
            latest_revision is not None
            and latest_revision != current_revision
            and row["review_status"] != "approved"
        ):
            exclude(row, "changed_revision_needing_approval")
            continue
        if row["review_status"] not in AUTO_UPLOAD_ALLOWED_REVIEW_STATUSES:
            exclude(row, "blocked_review_status")
            continue

        hold_state = effective_hold_state(
            row["hold_state"], row["embargo_until"], now=current_time
        )
        if hold_state not in SHAREABLE_HOLD_STATES:
            exclude(row, "held_or_embargoed")
            continue
        if not _blob_present_for_revision(row["session_id"], row["blob_path"]):
            exclude(row, "missing_blob")
            continue
        if not _auto_upload_raw_source_resolvable(row["raw_source_path"]):
            exclude(row, "raw_source_unavailable")
            continue

        candidate = dict(row)
        candidate.pop("hold_state", None)
        candidate.pop("embargo_until", None)
        candidate.pop("already_shared_exact_revision", None)
        candidate["completion_mode"] = completion_mode
        candidate["updated_since_last_share"] = latest_revision is not None
        candidate["_end_timestamp"] = end_time.timestamp()
        eligible.append(candidate)

    eligible.sort(
        key=lambda row: (
            row["ai_failure_value_score"] is None,
            -(row["ai_failure_value_score"] or 0),
            -row["_end_timestamp"],
            row["session_id"],
        )
    )
    for candidate in eligible:
        candidate.pop("_end_timestamp", None)
    selected = eligible[:limit]
    base_report.update({
        "eligible": eligible,
        "selected": selected,
        "eligible_count": len(eligible),
        "selected_count": len(selected),
        "deferred_by_cap": max(0, len(eligible) - len(selected)),
        "exclusions": exclusions,
    })
    return base_report


def revision_review_blockers(
    conn: sqlite3.Connection,
    session_ids: list[str],
) -> list[dict[str, Any]]:
    """Return re-shared revisions that have not received fresh approval."""
    if not session_ids:
        return []
    placeholders = ", ".join("?" for _ in session_ids)
    rows = conn.execute(
        _LATEST_SUCCESSFUL_REVISIONS_CTE
        + "SELECT s.session_id, s.review_status, "
        "s.content_revision AS revision_hash, "
        "latest.content_revision AS last_shared_revision_hash "
        "FROM sessions s "
        "JOIN latest_shared_revisions latest ON latest.session_id = s.session_id "
        f"WHERE s.session_id IN ({placeholders}) "
        "AND s.content_revision IS NOT latest.content_revision "
        "AND s.review_status != 'approved' "
        "ORDER BY s.session_id",
        session_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def auto_upload_review_blockers(
    conn: sqlite3.Connection,
    session_ids: list[str],
) -> list[dict[str, Any]]:
    """Return selected traces that no longer clear automatic review gates.

    First-time traces may remain ``new``, ``shortlisted``, or ``approved``.
    A changed revision of a previously shared trace must remain ``approved``
    through the final submission transition.
    """
    ordered_ids = list(dict.fromkeys(session_ids))
    if not ordered_ids:
        return []
    placeholders = ", ".join("?" for _ in ordered_ids)
    rows = conn.execute(
        "SELECT session_id, review_status FROM sessions "
        f"WHERE session_id IN ({placeholders})",
        ordered_ids,
    ).fetchall()
    by_id = {str(row["session_id"]): row for row in rows}
    blockers: list[dict[str, Any]] = []
    blocked_ids: set[str] = set()
    for session_id in ordered_ids:
        row = by_id.get(session_id)
        if row is None:
            blockers.append({
                "session_id": session_id,
                "review_status": None,
                "reason": "missing",
            })
            blocked_ids.add(session_id)
        elif row["review_status"] not in AUTO_UPLOAD_ALLOWED_REVIEW_STATUSES:
            blockers.append({
                "session_id": session_id,
                "review_status": row["review_status"],
                "reason": "blocked_review_status",
            })
            blocked_ids.add(session_id)

    for blocker in revision_review_blockers(conn, ordered_ids):
        session_id = str(blocker["session_id"])
        if session_id in blocked_ids:
            continue
        blockers.append({**blocker, "reason": "changed_revision_needing_approval"})
    return blockers


def already_shared_revision_blockers(
    conn: sqlite3.Connection,
    session_ids: list[str],
) -> list[dict[str, Any]]:
    """Return selected traces whose exact current revision already uploaded."""
    if not session_ids:
        return []
    placeholders = ", ".join("?" for _ in session_ids)
    rows = conn.execute(
        _LATEST_SUCCESSFUL_REVISIONS_CTE
        + "SELECT s.session_id, s.content_revision AS revision_hash, "
        "latest.content_revision AS last_shared_revision_hash "
        "FROM sessions s "
        "JOIN latest_shared_revisions latest ON latest.session_id = s.session_id "
        f"WHERE s.session_id IN ({placeholders}) "
        f"AND ({_SUCCESSFUL_EXACT_REVISION_EXISTS_SQL}) "
        "ORDER BY s.session_id",
        session_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def share_predecessor_blockers(
    conn: sqlite3.Connection,
    share_id: str,
) -> list[dict[str, Any]]:
    """Return packaged replacements based on a stale uploaded predecessor.

    The share remains valid when the latest successful revision is the
    predecessor captured at creation. If a different share already uploaded
    this share's own revision, it is a known duplicate. Any third revision
    means a newer replacement won the race and this share must not overwrite
    it.
    """
    rows = conn.execute(
        "SELECT session_id, content_revision, replaces_revision "
        "FROM share_sessions WHERE share_id = ? ORDER BY session_id",
        (share_id,),
    ).fetchall()
    blockers: list[dict[str, Any]] = []
    for row in rows:
        latest = _latest_successful_revision(
            conn,
            row["session_id"],
            exclude_share_id=share_id,
        )
        if latest is None:
            continue
        if latest == row["content_revision"]:
            reason = "already_shared_revision"
        elif latest == row["replaces_revision"]:
            continue
        else:
            reason = "stale_predecessor"
        blockers.append({
            "session_id": row["session_id"],
            "revision_hash": row["content_revision"],
            "replaces_revision_hash": row["replaces_revision"],
            "latest_shared_revision_hash": latest,
            "reason": reason,
        })
    return blockers


def get_share_ready_stats(
    conn: sqlite3.Connection,
    *,
    excluded_projects: list[str] | None = None,
    source_filter: str | list[str] | tuple[str, ...] | None = None,
    include_unapproved: bool = False,
) -> dict[str, Any]:
    """Return sessions whose current content revision has not been shared.

    By default returns only `review_status='approved'` sessions; pass
    `include_unapproved=True` to widen the pool to new, shortlisted, and
    approved sessions so the Preview UI can offer never-shared sessions for
    explicit review. Blocked and segmented sessions are never share-ready. A
    changed revision of a previously shared trace still requires fresh
    approval before it is offered. Recommendations are ranked by failure value
    first so the share wizard starts with the best failure examples.
    """
    where_clauses: list[str] = []
    params: list[Any] = []
    if include_unapproved:
        where_clauses.append(
            "s.review_status IN ('new', 'shortlisted', 'approved')"
        )
    else:
        where_clauses.append("s.review_status = 'approved'")
    if source_filter is not None:
        if isinstance(source_filter, (list, tuple)):
            values = [s for s in source_filter if s]
            if values:
                where_clauses.append(f"s.source IN ({','.join('?' for _ in values)})")
                params.extend(values)
        else:
            where_clauses.append("s.source = ?")
            params.append(source_filter)
    # A current revision is eligible when no successful snapshot exists or it
    # differs from the latest successful snapshot. Changed previously-shared
    # traces are never exposed before fresh approval, even in the widened pool.
    where_clauses.extend([
        "(latest.content_revision IS NULL "
        "OR s.content_revision IS NOT latest.content_revision)",
        "NOT (latest.content_revision IS NOT NULL "
        "AND s.content_revision IS NOT latest.content_revision "
        "AND s.review_status != 'approved')",
    ])
    where_sql = f" WHERE {' AND '.join(where_clauses)}"
    rows = conn.execute(
        _LATEST_SUCCESSFUL_REVISIONS_CTE
        + "SELECT s.session_id, s.project, s.model, s.source, s.display_title,"
        " s.ai_quality_score, s.ai_failure_value_score, s.ai_recovery_labels,"
        " s.ai_failure_attribution, s.ai_failure_modes, s.ai_learning_summary,"
        " s.user_messages, s.assistant_messages, s.tool_uses,"
        f" s.input_tokens, s.output_tokens, ({_OUTCOME_NORMALIZE_SQL}) as outcome_badge, s.client_origin,"
        " s.runtime_channel, s.start_time, s.review_status, s.hold_state, s.embargo_until,"
        " s.content_revision AS revision_hash,"
        " latest.content_revision AS last_shared_revision_hash,"
        " CASE WHEN latest.content_revision IS NOT NULL "
        "      AND s.content_revision IS NOT latest.content_revision "
        "      THEN 1 ELSE 0 END AS updated_since_last_share"
        " FROM sessions s"
        " LEFT JOIN latest_shared_revisions latest ON latest.session_id = s.session_id"
        f"{where_sql}"
        " ORDER BY (s.ai_failure_value_score IS NULL),"
        " s.ai_failure_value_score DESC, s.start_time DESC,"
        " (s.review_status = 'approved') DESC, s.ai_quality_score DESC",
        params,
    ).fetchall()
    cols = ["session_id", "project", "model", "source", "display_title",
            "ai_quality_score", "ai_failure_value_score", "ai_recovery_labels",
            "ai_failure_attribution", "ai_failure_modes", "ai_learning_summary",
            "user_messages", "assistant_messages", "tool_uses", "input_tokens",
            "output_tokens", "outcome_badge", "client_origin", "runtime_channel",
            "start_time", "review_status", "hold_state", "embargo_until",
            "revision_hash", "last_shared_revision_hash",
            "updated_since_last_share"]
    sessions = [dict(zip(cols, r)) for r in rows]
    for session in sessions:
        session["updated_since_last_share"] = bool(
            session["updated_since_last_share"]
        )
        for field in ("ai_recovery_labels", "ai_failure_modes"):
            if isinstance(session.get(field), str):
                try:
                    session[field] = json.loads(session[field])
                except (json.JSONDecodeError, ValueError):
                    session[field] = []
    # Only offer sessions that are actually shareable: drop explicit holds
    # (`pending_review`, active `embargoed`) so the student cannot pick a
    # session that the submit-time release gate would later reject. Auto-expired
    # embargoes pass through via `effective_hold_state`. The two helper columns
    # are consumed here and removed so the response shape is unchanged.
    # NOTE: this runs before the recommendation pool and the projects/models
    # sets are computed below, so all of those are derived from the shareable
    # subset only (a project whose sessions are all held won't be offered).
    shareable_sessions: list[dict[str, Any]] = []
    for session in sessions:
        effective = effective_hold_state(
            session.pop("hold_state", None), session.pop("embargo_until", None)
        )
        if effective in SHAREABLE_HOLD_STATES:
            shareable_sessions.append(session)
    sessions = shareable_sessions
    if excluded_projects:
        sessions = [
            session for session in sessions
            if not session_matches_excluded_projects(session, excluded_projects)
        ]
    projects: set[str] = set()
    models: set[str] = set()
    for s in sessions:
        if s.get("project"):
            projects.add(s["project"])
        if s.get("model"):
            models.add(s["model"])
    recommended_pool: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def _add_recommendation(session: dict[str, Any]) -> None:
        sid = session.get("session_id")
        if not sid or sid in seen_ids:
            return
        recommended_pool.append(session)
        seen_ids.add(sid)

    for session in sessions:
        if len(recommended_pool) >= SHARE_RECOMMENDATION_LIMIT:
            break
        if session.get("source") not in FAILURE_VALUE_SOURCE_SCOPE:
            continue
        if session.get("ai_failure_value_score") is None:
            continue
        _add_recommendation(session)

    recommended_ids = [s["session_id"] for s in recommended_pool[:SHARE_RECOMMENDATION_LIMIT]]

    return {
        "count": len(sessions),
        "total_approved": conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE review_status = 'approved'"
        ).fetchone()[0],
        "projects": sorted(projects),
        "models": sorted(models),
        "recommended_session_ids": recommended_ids,
        "sessions": sessions,
    }


def get_share(
    conn: sqlite3.Connection,
    share_id: str,
) -> dict[str, Any] | None:
    """Get share detail with linked session metadata.

    Returns None if the share is not found.
    """
    row = conn.execute(
        "SELECT * FROM shares WHERE share_id = ?",
        (share_id,),
    ).fetchone()
    if row is None:
        return None

    result = dict(row)
    _with_legacy_bundle_alias(result)

    # Fetch linked sessions
    session_rows = conn.execute(
        "SELECT s.*, bs.content_revision AS share_content_revision, "
        "bs.replaces_revision AS share_replaces_revision "
        "FROM share_sessions bs"
        " JOIN sessions s ON s.session_id = bs.session_id"
        " WHERE bs.share_id = ?"
        " ORDER BY s.start_time ASC, bs.added_at ASC",
        (share_id,),
    ).fetchall()
    if not session_rows:
        session_rows = conn.execute(
            "SELECT * FROM sessions WHERE share_id = ? ORDER BY start_time ASC",
            (share_id,),
        ).fetchall()
    result["sessions"] = [dict(r) for r in session_rows]

    # Parse manifest JSON if present
    if result.get("manifest"):
        try:
            result["manifest"] = json.loads(result["manifest"])
        except (json.JSONDecodeError, ValueError):
            pass

    return result


EXPORT_FIELDS = {
    "session_id", "project", "source", "model",
    "start_time", "end_time", "duration_seconds",
    "git_branch",
    "user_messages", "assistant_messages", "tool_uses",
    "input_tokens", "output_tokens",
    "display_title", "messages",
    "outcome_badge", "value_badges", "risk_badges",
    "ai_quality_score", "ai_failure_value_score", "ai_recovery_labels",
    "ai_failure_attribution", "ai_failure_modes", "ai_learning_summary",
    "ai_scoring_detail", "task_type",
    "revision_hash", "replaces_revision_hash",
    # NOTE: files_touched and commands_run are intentionally excluded from
    # exports — they contain unredacted file paths and shell commands that
    # could leak internal project structure or sensitive information.
}


def build_trufflehog_blocked_sessions(
    manifest: dict[str, Any],
    report: Any,
) -> list[dict[str, Any]]:
    """Map TruffleHog JSONL line findings back to exported sessions.

    ``sessions.jsonl`` is one line per exported session. If every finding has
    a valid line number, the UI can offer an explicit "remove these traces and
    retry" recovery path. If any finding cannot be mapped, return an empty
    list so callers keep the existing hard block.
    """
    findings = list(getattr(report, "findings", []) or [])
    if not findings:
        return []

    sessions = manifest.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        return []

    blocked_by_id: dict[str, dict[str, Any]] = {}
    for finding in findings:
        line = getattr(finding, "line", None)
        if not isinstance(line, int) or line < 1 or line > len(sessions):
            return []

        session = sessions[line - 1]
        if not isinstance(session, dict):
            return []
        session_id = session.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return []

        entry = blocked_by_id.setdefault(
            session_id,
            {
                "session_id": session_id,
                "project": session.get("project"),
                "source": session.get("source"),
                "model": session.get("model"),
                "line": line,
                "findings": [],
            },
        )
        entry["findings"].append({
            "line": line,
            "detector": getattr(finding, "detector", None),
            "status": getattr(finding, "status", None),
            "masked": getattr(finding, "masked", None),
        })

    return sorted(blocked_by_id.values(), key=lambda item: item["line"])


def export_share_to_disk(
    conn: sqlite3.Connection,
    share_id: str,
    share: dict[str, Any],
    *,
    output_path: str | None = None,
    custom_strings: list[str] | None = None,
    extra_usernames: list[str] | None = None,
    excluded_projects: list[str] | None = None,
    blocked_domains: list[str] | None = None,
    allowlist_entries: list[dict[str, Any]] | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    """Export a share's sessions to disk as JSONL + manifest.

    Returns (export_dir, manifest). Returns (None, {}) if output_path
    validation fails.
    """
    if output_path:
        export_dir = Path(output_path).resolve()
        home = Path.home().resolve()
        if not export_dir.is_relative_to(home) and not export_dir.is_relative_to(Path("/tmp").resolve()):
            return None, {}
    else:
        export_dir = CONFIG_DIR / "shares" / share_id
    export_dir.mkdir(parents=True, exist_ok=True)

    sessions_file = export_dir / "sessions.jsonl"
    tmp_sessions_file = export_dir / "sessions.jsonl.tmp"
    manifest: dict[str, Any] = {
        "share_id": share_id,
        "bundle_id": share_id,
        "export_path": str(export_dir),
        "session_count": share.get("session_count", 0),
        "attestation": share.get("attestation"),
        "submission_note": share.get("submission_note"),
        "sessions": [],
    }

    total_redactions = 0
    redaction_types: dict[str, int] = {}

    def block_revision_conflicts(
        blockers: list[dict[str, Any]],
    ) -> tuple[Path, dict[str, Any]]:
        # This is deliberately in-memory only. A re-export attempt after a
        # trace grows must not overwrite or delete the already-reviewed,
        # pinned artifact for the older revision.
        manifest["blocked"] = True
        manifest["block_reason"] = "revision_conflict"
        manifest["block_message"] = (
            "One or more traces changed after selection. Review the updated "
            "trace and create a new share."
        )
        manifest["blocked_sessions"] = blockers
        return export_dir, manifest

    stale_rows = share_revision_blockers(conn, share_id)
    if stale_rows:
        return block_revision_conflicts(stale_rows)

    # Re-read the linked rows so the export uses authoritative create-time
    # snapshots even if its caller holds an older share dictionary.
    current_share = get_share(conn, share_id)
    selected_sessions = (
        current_share.get("sessions", []) if current_share is not None else []
    )
    prepared: list[tuple[dict[str, Any], dict[str, Any], str, str | None]] = []
    skipped_session_ids: list[str] = []
    preflight_blockers: list[dict[str, Any]] = []
    for selected in selected_sessions:
        session_id = selected["session_id"]
        if session_matches_excluded_projects(selected, excluded_projects):
            skipped_session_ids.append(session_id)
            continue
        detail = get_session_detail(conn, session_id)
        if detail is None:
            skipped_session_ids.append(session_id)
            continue
        expected_revision = selected.get("share_content_revision")
        actual_revision = compute_content_revision(detail)
        if actual_revision != expected_revision:
            preflight_blockers.append({
                "session_id": session_id,
                "expected_revision_hash": expected_revision,
                "current_revision_hash": actual_revision,
                "review_status": selected.get("review_status"),
            })
            continue
        prepared.append((
            selected,
            detail,
            actual_revision,
            selected.get("share_replaces_revision"),
        ))
    if preflight_blockers:
        return block_revision_conflicts(preflight_blockers)

    try:
        with open(tmp_sessions_file, "w") as f:
            for selected, detail, revision_hash, replaces_revision_hash in prepared:
                detail, n_redacted, redaction_log = apply_share_redactions(
                    conn,
                    detail,
                    custom_strings=custom_strings,
                    user_allowlist=allowlist_entries,
                    extra_usernames=extra_usernames,
                    blocked_domains=blocked_domains,
                )
                total_redactions += n_redacted
                for entry in redaction_log:
                    rtype = entry.get("type", "unknown")
                    redaction_types[rtype] = redaction_types.get(rtype, 0) + 1
                # Custom string redactions are counted in n_redacted but
                # don't produce log entries — track them separately.
                custom_count = n_redacted - len(redaction_log)
                if custom_count > 0:
                    redaction_types["custom"] = (
                        redaction_types.get("custom", 0) + custom_count
                    )
                clean = {k: v for k, v in detail.items() if k in EXPORT_FIELDS}
                clean["revision_hash"] = revision_hash
                clean["replaces_revision_hash"] = replaces_revision_hash
                f.write(json.dumps(clean, default=str) + "\n")
                manifest["sessions"].append({
                    "session_id": clean.get("session_id") or selected["session_id"],
                    "project": clean.get("project"),
                    "source": clean.get("source"),
                    "model": clean.get("model"),
                    "revision_hash": revision_hash,
                    "replaces_revision_hash": replaces_revision_hash,
                    # Aggregated counts per §Bundle manifest provenance —
                    # no hashes, plaintext, or offsets.
                    "redactions": build_session_redactions_summary(
                        conn, selected["session_id"],
                    ),
                })
        os.replace(tmp_sessions_file, sessions_file)
    except BaseException:
        tmp_sessions_file.unlink(missing_ok=True)
        raise

    # Update count to match actually exported sessions (some may have missing blobs)
    manifest["session_count"] = len(manifest["sessions"])
    manifest["redaction_summary"] = {
        "total_redactions": total_redactions,
        "by_type": redaction_types,
    }

    # Mandatory post-redaction scan — independent oracle against our
    # own redactor. Any finding (or missing binary) blocks the share.
    from ..redaction import trufflehog as trufflehog_scanner

    trufflehog_report = trufflehog_scanner.scan_file(sessions_file)
    # Stamp which engine version scanned so the manifest/report make
    # staleness auditable per share (the managed binary can drift from
    # the source pin between installs).
    trufflehog_report.engine = trufflehog_scanner.engine_fingerprint()
    trufflehog_scanner.write_report(export_dir / "trufflehog.json", trufflehog_report)
    manifest["redaction_summary"]["trufflehog"] = trufflehog_report.summary()

    # A successful share baseline must describe only emitted sessions. Remove
    # selections filtered out during packaging instead of letting their
    # create-time snapshots masquerade as uploaded revisions later. Defer DB
    # mutation until all scanners complete so an exception leaves no open
    # partial selection change.
    for session_id in skipped_session_ids:
        conn.execute(
            "DELETE FROM share_sessions WHERE share_id = ? AND session_id = ?",
            (share_id, session_id),
        )
        conn.execute(
            "UPDATE sessions SET share_id = NULL "
            "WHERE session_id = ? AND share_id = ?",
            (session_id, share_id),
        )

    if trufflehog_report.blocking:
        manifest["blocked"] = True
        manifest["block_reason"] = trufflehog_report.block_reason
        manifest["block_message"] = trufflehog_scanner.format_block_message(trufflehog_report)
        blocked_sessions = build_trufflehog_blocked_sessions(manifest, trufflehog_report)
        if blocked_sessions:
            manifest["blocked_sessions"] = blocked_sessions
        with open(export_dir / "manifest.json", "w") as f:
            json.dump(manifest, f, indent=2, default=str)
        # Record the block on the share row so the UI can surface it,
        # but do NOT advance status to shared/exported — that would
        # silently imply the share is clean.
        conn.execute(
            "UPDATE shares SET session_count = ?, manifest = ? WHERE share_id = ?",
            (manifest["session_count"], json.dumps(manifest, default=str), share_id),
        )
        conn.commit()
        return export_dir, manifest

    with open(export_dir / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2, default=str)

    next_status = "shared" if share.get("status") == "shared" else "exported"
    conn.execute(
        "UPDATE shares SET status = ?, session_count = ?, manifest = ? "
        "WHERE share_id = ?",
        (
            next_status,
            manifest["session_count"],
            json.dumps(manifest, default=str),
            share_id,
        ),
    )
    conn.commit()

    return export_dir, manifest


def get_policies(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all policy rules."""
    rows = conn.execute(
        "SELECT * FROM policies ORDER BY created_at ASC"
    ).fetchall()
    return [dict(row) for row in rows]


def add_policy(
    conn: sqlite3.Connection,
    policy_type: str,
    value: str,
    reason: str | None = None,
) -> str:
    """Add a policy rule. Returns the new policy_id."""
    policy_id = str(uuid.uuid4())
    now = _now_iso()

    conn.execute(
        """INSERT INTO policies (policy_id, policy_type, value, reason, created_at)
        VALUES (?, ?, ?, ?, ?)""",
        (policy_id, policy_type, value, reason, now),
    )
    mark_auto_upload_profile_changed(conn)
    conn.commit()
    return policy_id


def remove_policy(conn: sqlite3.Connection, policy_id: str) -> bool:
    """Remove a policy rule. Returns True if it existed and was removed."""
    cursor = conn.execute(
        "DELETE FROM policies WHERE policy_id = ?",
        (policy_id,),
    )
    if cursor.rowcount > 0:
        mark_auto_upload_profile_changed(conn)
    conn.commit()
    return cursor.rowcount > 0
