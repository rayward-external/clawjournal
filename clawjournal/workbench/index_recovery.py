"""Startup integrity guard and guided recovery for the workbench index.

The workbench SQLite database mixes rebuildable index data with durable user
decisions.  Recovery therefore never deletes a damaged database silently:
the authenticated UI must request it, the exact database sidecars are backed
up first, and safety-relevant state is copied into the rebuilt index whenever
it can be read.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import index as index_module

logger = logging.getLogger(__name__)

RECOVERY_MARKER_FILENAME = index_module.INDEX_RECOVERY_MARKER_FILENAME

_STATE_LOCK = threading.Lock()
_INDEX_HEALTH: dict[str, Any] = {
    "status": "ready",
    "message": "The workbench index is ready.",
}

_SESSION_STATE_COLUMNS = (
    "session_id",
    "project",
    "source",
    "display_title",
    "review_status",
    "selection_reason",
    "reviewer_notes",
    "reviewed_at",
    "blob_path",
    "raw_source_path",
    "indexed_at",
    "updated_at",
    "share_id",
    "hold_state",
    "embargo_until",
    "content_revision",
)

_CRITICAL_SESSION_STATE_COLUMNS = frozenset({
    "session_id",
    "review_status",
    "hold_state",
    "content_revision",
})

_CRITICAL_FINDING_DECISION_COLUMNS = frozenset({
    "session_id",
    "engine",
    "entity_hash",
    "status",
    "revision",
})

_FINDING_DECISION_COLUMNS = (
    "session_id",
    "engine",
    "entity_hash",
    "status",
    "decided_by",
    "decision_source_id",
    "decided_at",
    "decision_reason",
    "revision",
)

_DURABLE_TABLES = (
    "policies",
    "findings_allowlist",
    "shares",
    "share_sessions",
    "session_hold_history",
    "auto_upload_enrollment",
    "auto_upload_enrollment_job",
    "benchmarks",
    "benchmark_tasks",
    "benchmark_exports",
)

_EVENT_SESSION_COLUMNS = (
    "id",
    "session_key",
    "parent_session_key",
    "parent_session_id",
    "client",
    "client_version",
    "started_at",
    "ended_at",
    "status",
)
_EVENT_COLUMNS = (
    "id",
    "session_id",
    "type",
    "event_key",
    "event_at",
    "ingested_at",
    "source",
    "source_path",
    "source_offset",
    "seq",
    "client",
    "confidence",
    "lossiness",
    "raw_json",
)
_EVENT_OVERRIDE_COLUMNS = (
    "session_id",
    "event_key",
    "type",
    "source",
    "confidence",
    "lossiness",
    "event_at",
    "payload_json",
    "origin",
    "created_at",
    "write_seq",
)
_EVENT_SNIPPET_COLUMNS = (
    "source_path",
    "source_offset",
    "seq",
    "text",
    "imported_at",
)
_CAPTURE_CURSOR_COLUMNS = (
    "consumer_id",
    "source_path",
    "inode",
    "last_offset",
    "last_modified",
    "client",
    "first_seen",
    "last_seen",
)
_TOKEN_USAGE_COLUMNS = (
    "event_id",
    "session_id",
    "model",
    "model_family",
    "model_tier",
    "model_provider",
    "input",
    "output",
    "cache_read",
    "cache_write",
    "reasoning",
    "service_tier",
    "data_source",
    "cost_estimate",
    "pricing_table_version",
    "event_at",
)
_COST_ANOMALY_COLUMNS = (
    "id",
    "session_id",
    "turn_event_id",
    "kind",
    "confidence",
    "evidence_json",
    "created_at",
)
_COST_INGEST_STATE_COLUMNS = ("consumer_id", "last_event_id")
_INCIDENT_COLUMNS = (
    "id",
    "session_id",
    "kind",
    "first_event_id",
    "last_event_id",
    "evidence_json",
    "count",
    "confidence",
    "created_at",
)
_LOOP_INGEST_STATE_COLUMNS = (
    "consumer_id",
    "last_event_id",
    "last_override_write_seq",
    "last_override_created_at",
    "last_override_session_id",
    "last_override_event_key",
)

_EVENT_REQUIRED_COLUMNS = frozenset({
    "id",
    "session_id",
    "type",
    "ingested_at",
    "source",
    "source_path",
    "source_offset",
    "client",
    "confidence",
    "lossiness",
    "raw_json",
})
_EVENT_SESSION_REQUIRED_COLUMNS = frozenset({"id", "session_key", "client"})
_EVENT_OVERRIDE_REQUIRED_COLUMNS = frozenset({
    "session_id",
    "event_key",
    "type",
    "source",
    "confidence",
    "lossiness",
    "payload_json",
    "created_at",
})
_EVENT_SNIPPET_REQUIRED_COLUMNS = frozenset(_EVENT_SNIPPET_COLUMNS)
_CAPTURE_CURSOR_REQUIRED_COLUMNS = frozenset(_CAPTURE_CURSOR_COLUMNS)
_TOKEN_USAGE_REQUIRED_COLUMNS = frozenset({
    "event_id",
    "session_id",
    "data_source",
})
_COST_ANOMALY_REQUIRED_COLUMNS = frozenset(_COST_ANOMALY_COLUMNS)
_COST_INGEST_STATE_REQUIRED_COLUMNS = frozenset(_COST_INGEST_STATE_COLUMNS)
_INCIDENT_REQUIRED_COLUMNS = frozenset(_INCIDENT_COLUMNS)
_LOOP_INGEST_STATE_REQUIRED_COLUMNS = frozenset({
    "consumer_id",
    "last_event_id",
})

_EXECUTION_RECORDER_TABLES = frozenset({
    "event_sessions",
    "events",
    "event_overrides",
    "event_source_snippets",
    "token_usage",
    "cost_anomalies",
    "cost_ingest_state",
    "incidents",
    "loop_ingest_state",
    "capture_cursors",
})
_NON_SAFETY_RELATIONAL_TABLES = _EXECUTION_RECORDER_TABLES | frozenset({
    "benchmarks",
    "benchmark_tasks",
    "benchmark_exports",
})


class UnsafeIndexRecovery(RuntimeError):
    """The damaged index cannot be rebuilt automatically without data loss."""


def _index_path() -> Path:
    return Path(str(index_module.INDEX_DB))


def _marker_path(database: Path) -> Path:
    return database.parent / RECOVERY_MARKER_FILENAME


def _fsync_directory(directory: Path) -> None:
    """Persist directory-entry changes where the platform exposes that primitive."""

    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_marker(database: Path, payload: dict[str, Any]) -> None:
    marker = _marker_path(database)
    temporary = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, marker)
        _fsync_directory(marker.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _load_marker(database: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(_marker_path(database).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _set_health(payload: dict[str, Any]) -> dict[str, Any]:
    with _STATE_LOCK:
        _INDEX_HEALTH.clear()
        _INDEX_HEALTH.update(payload)
        return dict(_INDEX_HEALTH)


def current_index_health() -> dict[str, Any]:
    with _STATE_LOCK:
        return dict(_INDEX_HEALTH)


def begin_index_health_check() -> dict[str, Any]:
    """Publish the fail-closed startup state before HTTP begins serving."""

    database = _index_path()
    return _set_health({
        "status": "checking",
        "message": "Checking the local index before enabling database work...",
        "database_path": str(database.resolve()),
        "sqlite_version": sqlite3.sqlite_version,
        "journal_mode": index_module.INDEX_JOURNAL_MODE,
        "automatic_recovery_available": False,
    })


def recovery_marker_exists(path: Path | None = None) -> bool:
    """Return whether any process has published an index-recovery marker."""

    database = Path(path) if path is not None else _index_path()
    return _marker_path(database).exists()


def synchronize_index_health() -> dict[str, Any]:
    """Refresh cached health only when another process changed the marker.

    The ordinary request path deliberately avoids rerunning SQLite integrity
    checks.  A marker transition is rare and is the cross-process signal that
    an already-running daemon must fail closed or resume after recovery.
    """

    health = current_index_health()
    marker_exists = recovery_marker_exists()
    if (
        marker_exists
        and health.get("status") not in {"checking", "rebuilding"}
        and health.get("interrupted_recovery") is not True
    ):
        return initialize_index_health()
    if (
        not marker_exists
        and health.get("status") != "rebuilding"
        and health.get("interrupted_recovery") is True
    ):
        return initialize_index_health()
    return health


def begin_guided_rebuild() -> dict[str, Any]:
    """Reserve the worker and publish the cross-process gate before returning."""

    with _STATE_LOCK:
        if _INDEX_HEALTH.get("status") == "rebuilding":
            return dict(_INDEX_HEALTH)
        if _INDEX_HEALTH.get("status") != "recovery_required":
            raise UnsafeIndexRecovery("The workbench index does not need recovery.")
        if _INDEX_HEALTH.get("automatic_recovery_available") is not True:
            raise UnsafeIndexRecovery(
                "Automatic recovery is not available for this index state."
            )
        database = _index_path()
        marker = _load_marker(database)
        if marker is None:
            marker = {
                "version": 1,
                "database_path": str(database.resolve()),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "stage": "draining",
            }
            _write_marker(database, marker)
        _INDEX_HEALTH.update({
            "status": "rebuilding",
            "stage": "draining",
            "message": "Waiting for active index work to finish safely...",
        })
        return dict(_INDEX_HEALTH)


def _read_only_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro",
        uri=True,
        timeout=5,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _exclusive_source_connection(path: Path) -> sqlite3.Connection:
    """Hold the source stable while its decisions and exact files are copied."""

    conn = sqlite3.connect(
        path.resolve().as_uri() + "?mode=rw",
        uri=True,
        timeout=5,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("BEGIN EXCLUSIVE")
    except Exception:
        conn.close()
        raise
    return conn


def _health_connection(path: Path) -> sqlite3.Connection:
    """Open for checks while allowing SQLite's own hot-journal rollback.

    DELETE journaling can leave a rollback journal after process or machine
    failure. SQLite must briefly have a read/write handle to roll that
    incomplete transaction back before any reader can inspect the database.
    ``query_only`` still prevents this health-check connection from issuing
    application writes. Recovery snapshots use ``_read_only_connection`` and
    therefore never alter a damaged source or its backup.
    """

    conn = index_module.open_existing_index(
        database=path,
        timeout=5,
    )
    conn.execute("PRAGMA query_only=ON")
    return conn


def _is_storage_or_lock_error(exc: sqlite3.DatabaseError) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "database is locked",
            "database table is locked",
            "database is busy",
            "disk i/o error",
            "unable to open database file",
            "readonly database",
            "database or disk is full",
            "permission denied",
        )
    )


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    # Table names come only from the fixed constants above.
    return {
        str(row[1])
        for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _select_columns(
    conn: sqlite3.Connection,
    table: str,
    requested: tuple[str, ...],
    *,
    where: str = "",
    required: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    available = _table_columns(conn, table)
    missing = required - available
    if missing:
        raise sqlite3.DatabaseError(
            f'{table} is missing recovery column(s): {", ".join(sorted(missing))}'
        )
    columns = [column for column in requested if column in available]
    if not columns:
        return []
    sql = f'SELECT {", ".join(columns)} FROM "{table}"'
    if where:
        sql += f" WHERE {where}"
    return [dict(row) for row in conn.execute(sql).fetchall()]


def _select_all(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"').fetchall()]


def _recovery_snapshot(
    conn: sqlite3.Connection,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    snapshot: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    try:
        tables = _table_names(conn)
    except sqlite3.DatabaseError:
        return {}, ["database schema"]

    try:
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if any(
            str(row[0]) not in _NON_SAFETY_RELATIONAL_TABLES
            for row in violations
        ):
            errors.append("relational safety state")
    except sqlite3.DatabaseError:
        errors.append("relational safety state")

    if "sessions" not in tables:
        snapshot["sessions"] = []
        errors.append("session decisions and hold state")
    else:
        try:
            snapshot["sessions"] = _select_columns(
                conn,
                "sessions",
                _SESSION_STATE_COLUMNS,
                required=_CRITICAL_SESSION_STATE_COLUMNS,
            )
        except sqlite3.DatabaseError:
            snapshot["sessions"] = []
            errors.append("session decisions and hold state")

    if "findings" in tables:
        try:
            snapshot["finding_decisions"] = _select_columns(
                conn,
                "findings",
                _FINDING_DECISION_COLUMNS,
                where="status != 'open' OR decided_at IS NOT NULL",
                required=_CRITICAL_FINDING_DECISION_COLUMNS,
            )
        except sqlite3.DatabaseError:
            snapshot["finding_decisions"] = []
            errors.append("finding decisions")
    else:
        snapshot["finding_decisions"] = []

    for table in _DURABLE_TABLES:
        if table not in tables:
            snapshot[table] = []
            continue
        try:
            snapshot[table] = _select_all(conn, table)
        except sqlite3.DatabaseError:
            snapshot[table] = []
            errors.append(table.replace("_", " "))

    return snapshot, errors


def _snapshot_from_path(
    path: Path,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    conn: sqlite3.Connection | None = None
    try:
        conn = _read_only_connection(path)
        return _recovery_snapshot(conn)
    except (OSError, sqlite3.DatabaseError):
        return {}, ["database schema and durable safety state"]
    finally:
        if conn is not None:
            conn.close()


def inspect_index_health(path: Path | None = None) -> dict[str, Any]:
    database = Path(path) if path is not None else _index_path()
    base: dict[str, Any] = {
        "database_path": str(database.resolve()),
        "sqlite_version": sqlite3.sqlite_version,
        "journal_mode": index_module.INDEX_JOURNAL_MODE,
        "automatic_recovery_available": False,
    }
    marker = _load_marker(database)
    if marker is not None:
        raw_backup = marker.get("backup_path")
        if isinstance(raw_backup, str) and raw_backup:
            try:
                _backup_from_marker(database, marker)
            except UnsafeIndexRecovery as exc:
                return {
                    **base,
                    "status": "unavailable",
                    "message": (
                        "The interrupted-recovery backup is unavailable, so "
                        "ClawJournal will not replace the current index."
                    ),
                    "detail": str(exc),
                    "automatic_recovery_available": False,
                    "interrupted_recovery": True,
                }
            message = (
                "A previous index rebuild was interrupted. The original backup "
                "is still available and recovery can be retried safely."
            )
        elif marker.get("stage") in {"draining", "preparing"} and database.is_file():
            message = (
                "A previous index rebuild stopped before its backup completed. "
                "The original index has not been replaced and recovery can be "
                "retried safely."
            )
        else:
            detail = (
                "The original index is missing from the preparation stage."
                if marker.get("stage") in {"draining", "preparing"}
                else str(_marker_path(database).resolve())
            )
            return {
                **base,
                "status": "unavailable",
                "message": (
                    "The interrupted-recovery record does not identify a safe "
                    "backup or an untouched preparation stage."
                ),
                "detail": detail,
                "automatic_recovery_available": False,
                "interrupted_recovery": True,
            }
        return {
            **base,
            "status": "recovery_required",
            "message": message,
            "automatic_recovery_available": True,
            "backup_path": raw_backup if raw_backup else None,
            "interrupted_recovery": True,
        }
    if _marker_path(database).exists():
        return {
            **base,
            "status": "unavailable",
            "message": (
                "The interrupted-recovery record is unreadable, so ClawJournal "
                "will not guess which backup is authoritative."
            ),
            "detail": str(_marker_path(database).resolve()),
            "automatic_recovery_available": False,
            "interrupted_recovery": True,
        }
    if not database.exists():
        return {
            **base,
            "status": "ready",
            "message": "A new workbench index will be created.",
        }

    try:
        database_size = database.stat().st_size
    except OSError as exc:
        return {
            **base,
            "status": "unavailable",
            "message": "The workbench index storage is unavailable.",
            "detail": str(exc),
            "automatic_recovery_available": False,
        }
    if database_size == 0:
        return {
            **base,
            "status": "recovery_required",
            "message": "The workbench index is empty and must be rebuilt.",
            "detail": "The existing index.db file contains no SQLite data.",
            "automatic_recovery_available": True,
            "unreadable_state": ["database schema and durable safety state"],
            "recoverable_state_counts": {},
        }

    conn: sqlite3.Connection | None = None
    try:
        conn = _health_connection(database)
        journal_row = conn.execute("PRAGMA journal_mode").fetchone()
        if journal_row:
            base["journal_mode"] = str(journal_row[0]).lower()
        checks = [str(row[0]) for row in conn.execute("PRAGMA quick_check(1)")]
        foreign_key_error = conn.execute("PRAGMA foreign_key_check").fetchone()
        has_session_table = "sessions" in _table_names(conn)
        if checks == ["ok"] and foreign_key_error is None and has_session_table:
            return {
                **base,
                "status": "ready",
                "message": "The workbench index passed its integrity check.",
            }
        snapshot, errors = _recovery_snapshot(conn)
        counts = {name: len(rows) for name, rows in snapshot.items()}
        detail = checks[0] if checks != ["ok"] and checks else None
        if foreign_key_error is not None:
            detail = "The index contains an invalid foreign-key reference."
        elif not has_session_table:
            detail = "The index is missing its sessions table."
        return {
            **base,
            "status": "recovery_required",
            "message": "The workbench index is damaged and must be rebuilt.",
            "detail": detail or "SQLite integrity check failed.",
            "automatic_recovery_available": True,
            "unreadable_state": errors,
            "recoverable_state_counts": counts,
        }
    except (OSError, sqlite3.DatabaseError) as exc:
        if isinstance(exc, OSError) or _is_storage_or_lock_error(exc):
            return {
                **base,
                "status": "unavailable",
                "message": (
                    "The workbench index storage is locked or unavailable. "
                    "No automatic rebuild will be attempted."
                ),
                "detail": str(exc),
                "automatic_recovery_available": False,
            }
        errors: list[str] = ["database schema"]
        counts: dict[str, int] = {}
        if conn is not None:
            snapshot, errors = _recovery_snapshot(conn)
            counts = {name: len(rows) for name, rows in snapshot.items()}
        return {
            **base,
            "status": "recovery_required",
            "message": "The workbench index is unreadable and must be rebuilt.",
            "detail": str(exc),
            "automatic_recovery_available": True,
            "unreadable_state": errors,
            "recoverable_state_counts": counts,
        }
    finally:
        if conn is not None:
            conn.close()


def initialize_index_health() -> dict[str, Any]:
    """Inspect once at daemon startup, before any scanner or egress worker."""

    try:
        report = inspect_index_health()
    except Exception as exc:
        logger.exception("Workbench index startup check failed unexpectedly")
        return _set_health({
            "status": "unavailable",
            "message": "The workbench index could not be checked safely.",
            "detail": str(exc),
            "database_path": str(_index_path().resolve()),
            "sqlite_version": sqlite3.sqlite_version,
            "journal_mode": index_module.INDEX_JOURNAL_MODE,
            "automatic_recovery_available": False,
        })
    if report["status"] != "ready":
        return _set_health(report)
    try:
        conn = index_module.open_index()
        conn.close()
        report["journal_mode"] = index_module.INDEX_JOURNAL_MODE
    except Exception as exc:
        logger.exception("Workbench index bootstrap failed unexpectedly")
        report = {
            **report,
            "status": "unavailable",
            "message": "The workbench index could not be opened safely.",
            "detail": str(exc),
            "automatic_recovery_available": False,
        }
    return _set_health(report)


def _backup_index_files(database: Path) -> tuple[Path, list[Path]]:
    backup_root = database.parent / "index-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = backup_root / f"index-recovery-{timestamp}-{uuid.uuid4().hex[:8]}"
    backup_dir.mkdir(parents=False, exist_ok=False)
    _fsync_directory(backup_root)

    copied: list[Path] = []
    for source in (
        database,
        Path(str(database) + "-wal"),
        Path(str(database) + "-shm"),
        Path(str(database) + "-journal"),
    ):
        if not source.exists():
            continue
        destination = backup_dir / source.name
        shutil.copy2(source, destination)
        # Windows does not permit fsync on a read-only descriptor. Open the
        # completed copy read/write solely to force its bytes to stable storage.
        with destination.open("r+b") as file:
            os.fsync(file.fileno())
        copied.append(destination)
    if not copied:
        raise FileNotFoundError(f"No index files were found at {database}")
    _fsync_directory(backup_dir)
    return backup_dir, copied


def _remove_index_files(database: Path) -> None:
    for path in (
        database,
        Path(str(database) + "-wal"),
        Path(str(database) + "-shm"),
        Path(str(database) + "-journal"),
    ):
        path.unlink(missing_ok=True)


def _backup_from_marker(database: Path, marker: dict[str, Any]) -> tuple[Path, list[Path]]:
    raw_backup = marker.get("backup_path")
    if not isinstance(raw_backup, str) or not raw_backup:
        raise UnsafeIndexRecovery("The interrupted recovery backup path is missing.")
    backup_dir = Path(raw_backup).resolve()
    guarded_root = (database.parent / "index-backups").resolve()
    if not backup_dir.is_relative_to(guarded_root) or not backup_dir.is_dir():
        raise UnsafeIndexRecovery("The interrupted recovery backup path is invalid.")
    database_backup = backup_dir / database.name
    if not database_backup.is_file():
        raise UnsafeIndexRecovery(
            "The interrupted recovery database backup is missing."
        )
    backups = [database_backup]
    backups.extend(
        backup_dir / name
        for name in (
            database.name + "-wal",
            database.name + "-shm",
            database.name + "-journal",
        )
        if (backup_dir / name).is_file()
    )
    return backup_dir, backups


def _insert_rows(
    conn: sqlite3.Connection,
    table: str,
    rows: list[dict[str, Any]],
) -> int:
    if not rows:
        return 0
    available = _table_columns(conn, table)
    inserted = 0
    for row in rows:
        values = {key: value for key, value in row.items() if key in available}
        if not values:
            continue
        columns = list(values)
        placeholders = ", ".join("?" for _ in columns)
        quoted = ", ".join(f'"{column}"' for column in columns)
        conn.execute(
            f'INSERT OR REPLACE INTO "{table}" ({quoted}) VALUES ({placeholders})',
            [values[column] for column in columns],
        )
        inserted += 1
    return inserted


def _attached_table_names(
    conn: sqlite3.Connection,
    schema: str,
) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            f'SELECT name FROM "{schema}".sqlite_master WHERE type = \'table\''
        ).fetchall()
    }


def _attached_table_columns(
    conn: sqlite3.Connection,
    schema: str,
    table: str,
) -> set[str]:
    # Schema and table names come only from fixed constants in this module.
    return {
        str(row[1])
        for row in conn.execute(
            f'PRAGMA "{schema}".table_info("{table}")'
        ).fetchall()
    }


def _copy_attached_rows(
    conn: sqlite3.Connection,
    *,
    schema: str,
    table: str,
    requested: tuple[str, ...],
    required: frozenset[str],
    where: str = "",
    params: tuple[Any, ...] = (),
) -> int:
    source_columns = _attached_table_columns(conn, schema, table)
    missing = required - source_columns
    if missing:
        raise sqlite3.DatabaseError(
            f'{table} is missing recovery column(s): {", ".join(sorted(missing))}'
        )
    target_columns = _table_columns(conn, table)
    columns = [
        column
        for column in requested
        if column in source_columns and column in target_columns
    ]
    if not columns:
        return 0
    quoted = ", ".join(f'"{column}"' for column in columns)
    sql = (
        f'INSERT INTO main."{table}" ({quoted}) '
        f'SELECT {quoted} FROM "{schema}"."{table}"'
    )
    if where:
        sql += f" WHERE {where}"
    conn.execute(sql, params)
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _restore_execution_recorder(
    conn: sqlite3.Connection,
    source_database: Path,
) -> tuple[dict[str, int], list[str]]:
    """Restore non-rebuildable event data from the fsynced backup database.

    The large ``events.raw_json`` payload stays inside SQLite via
    ``INSERT ... SELECT``; it is never materialized as a Python ``fetchall``.
    Historical cost and incident rows are copied exactly; only FTS is rebuilt.
    """

    counts: dict[str, int] = {}
    warnings: list[str] = []
    if not source_database.is_file():
        return counts, warnings

    from clawjournal.capture.cursors import ensure_schema as ensure_cursor_schema
    from clawjournal.events.cost.schema import ensure_cost_schema
    from clawjournal.events.export.schema import ensure_export_schema
    from clawjournal.events.incidents.schema import ensure_incidents_schema
    from clawjournal.events.schema import ensure_schema as ensure_event_schema
    from clawjournal.events.view import ensure_view_schema

    ensure_event_schema(conn)
    ensure_view_schema(conn)
    ensure_cost_schema(conn)
    ensure_incidents_schema(conn)
    ensure_export_schema(conn)
    ensure_cursor_schema(conn)
    conn.commit()

    schema = "recovery_source"
    attached = False
    try:
        conn.execute(
            f'ATTACH DATABASE ? AS "{schema}"',
            (source_database.resolve().as_uri() + "?mode=ro",),
        )
        attached = True
        source_tables = _attached_table_names(conn, schema)
    except sqlite3.DatabaseError as exc:
        if attached:
            try:
                conn.execute(f'DETACH DATABASE "{schema}"')
            except sqlite3.DatabaseError:
                pass
        warnings.append(
            "Execution-recorder state could not be read and remains in the "
            f"index backup ({exc})."
        )
        return counts, warnings

    core_restored = False
    override_frontier_restored = False
    try:
        has_sessions = "event_sessions" in source_tables
        has_events = "events" in source_tables
        if has_sessions != has_events:
            warnings.append(
                "Execution-recorder state was incomplete and remains in the "
                "index backup."
            )
        elif has_sessions and has_events:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("PRAGMA defer_foreign_keys=ON")
                conn.execute("DELETE FROM events")
                conn.execute("DELETE FROM event_sessions")
                counts["event_sessions"] = _copy_attached_rows(
                    conn,
                    schema=schema,
                    table="event_sessions",
                    requested=_EVENT_SESSION_COLUMNS,
                    required=_EVENT_SESSION_REQUIRED_COLUMNS,
                )
                counts["events"] = _copy_attached_rows(
                    conn,
                    schema=schema,
                    table="events",
                    requested=_EVENT_COLUMNS,
                    required=_EVENT_REQUIRED_COLUMNS,
                )
                foreign_key_errors = [
                    row
                    for row in conn.execute("PRAGMA foreign_key_check").fetchall()
                    if str(row[0]) in {"event_sessions", "events"}
                ]
                if foreign_key_errors:
                    raise sqlite3.DatabaseError(
                        "restored execution-recorder rows violate a foreign key"
                    )
                conn.commit()
                core_restored = True
            except Exception as exc:
                conn.rollback()
                counts.pop("event_sessions", None)
                counts.pop("events", None)
                warnings.append(
                    "Execution-recorder state could not be restored and remains "
                    f"in the index backup ({exc})."
                )

            if core_restored and "event_overrides" in source_tables:
                conn.execute("SAVEPOINT restore_event_overrides")
                try:
                    source_override_columns = _attached_table_columns(
                        conn,
                        schema,
                        "event_overrides",
                    )
                    conn.execute("DELETE FROM event_overrides")
                    counts["event_overrides"] = _copy_attached_rows(
                        conn,
                        schema=schema,
                        table="event_overrides",
                        requested=_EVENT_OVERRIDE_COLUMNS,
                        required=_EVENT_OVERRIDE_REQUIRED_COLUMNS,
                    )
                    conn.execute("RELEASE SAVEPOINT restore_event_overrides")
                    # The loop detector can retain its exact frontier only
                    # when the source rows carried their original sequence.
                    # Legacy rows are restored, then assigned fresh sequences
                    # below, so their old cursor must be replayed from zero.
                    override_frontier_restored = (
                        "write_seq" in source_override_columns
                    )
                except Exception as exc:
                    conn.execute("ROLLBACK TO SAVEPOINT restore_event_overrides")
                    conn.execute("RELEASE SAVEPOINT restore_event_overrides")
                    counts.pop("event_overrides", None)
                    warnings.append(
                        "Execution-recorder overrides could not be restored and "
                        f"remain in the index backup ({exc})."
                    )
            elif core_restored:
                counts["event_overrides"] = 0

            if core_restored and "capture_cursors" in source_tables:
                conn.execute("SAVEPOINT restore_event_cursors")
                try:
                    conn.execute(
                        "DELETE FROM capture_cursors WHERE consumer_id = ?",
                        ("events",),
                    )
                    _copy_attached_rows(
                        conn,
                        schema=schema,
                        table="capture_cursors",
                        requested=_CAPTURE_CURSOR_COLUMNS,
                        required=_CAPTURE_CURSOR_REQUIRED_COLUMNS,
                        where='"consumer_id" = ?',
                        params=("events",),
                    )
                    counts["event_capture_cursors"] = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM capture_cursors "
                            "WHERE consumer_id = ?",
                            ("events",),
                        ).fetchone()[0]
                    )
                    conn.execute("RELEASE SAVEPOINT restore_event_cursors")
                except Exception as exc:
                    conn.execute("ROLLBACK TO SAVEPOINT restore_event_cursors")
                    conn.execute("RELEASE SAVEPOINT restore_event_cursors")
                    counts.pop("event_capture_cursors", None)
                    warnings.append(
                        "Execution-recorder cursors could not be restored and "
                        f"remain in the index backup ({exc})."
                    )

            cost_tables = {
                "token_usage",
                "cost_anomalies",
                "cost_ingest_state",
            }
            available_cost_tables = cost_tables & source_tables
            if core_restored and available_cost_tables == cost_tables:
                conn.execute("SAVEPOINT restore_event_cost")
                try:
                    conn.execute("DELETE FROM cost_anomalies")
                    conn.execute("DELETE FROM token_usage")
                    conn.execute("DELETE FROM cost_ingest_state")
                    counts["token_usage"] = _copy_attached_rows(
                        conn,
                        schema=schema,
                        table="token_usage",
                        requested=_TOKEN_USAGE_COLUMNS,
                        required=_TOKEN_USAGE_REQUIRED_COLUMNS,
                    )
                    counts["cost_anomalies"] = _copy_attached_rows(
                        conn,
                        schema=schema,
                        table="cost_anomalies",
                        requested=_COST_ANOMALY_COLUMNS,
                        required=_COST_ANOMALY_REQUIRED_COLUMNS,
                    )
                    counts["cost_ingest_state"] = _copy_attached_rows(
                        conn,
                        schema=schema,
                        table="cost_ingest_state",
                        requested=_COST_INGEST_STATE_COLUMNS,
                        required=_COST_INGEST_STATE_REQUIRED_COLUMNS,
                    )
                    conn.execute("RELEASE SAVEPOINT restore_event_cost")
                except Exception as exc:
                    conn.execute("ROLLBACK TO SAVEPOINT restore_event_cost")
                    conn.execute("RELEASE SAVEPOINT restore_event_cost")
                    for name in cost_tables:
                        counts.pop(name, None)
                    warnings.append(
                        "Historical event cost data could not be restored and "
                        f"remains in the index backup ({exc})."
                    )
            elif core_restored and available_cost_tables:
                warnings.append(
                    "Historical event cost state was incomplete and remains in "
                    "the index backup."
                )

            incident_tables = {"incidents", "loop_ingest_state"}
            available_incident_tables = incident_tables & source_tables
            if core_restored and available_incident_tables == incident_tables:
                conn.execute("SAVEPOINT restore_event_incidents")
                try:
                    reset_override_cursor = not override_frontier_restored
                    conn.execute("DELETE FROM incidents")
                    conn.execute("DELETE FROM loop_ingest_state")
                    counts["incidents"] = _copy_attached_rows(
                        conn,
                        schema=schema,
                        table="incidents",
                        requested=_INCIDENT_COLUMNS,
                        required=_INCIDENT_REQUIRED_COLUMNS,
                    )
                    counts["loop_ingest_state"] = _copy_attached_rows(
                        conn,
                        schema=schema,
                        table="loop_ingest_state",
                        requested=_LOOP_INGEST_STATE_COLUMNS,
                        required=_LOOP_INGEST_STATE_REQUIRED_COLUMNS,
                    )
                    if reset_override_cursor:
                        conn.execute(
                            "UPDATE loop_ingest_state SET "
                            "last_override_write_seq = 0, "
                            "last_override_created_at = NULL, "
                            "last_override_session_id = 0, "
                            "last_override_event_key = ''"
                        )
                    conn.execute("RELEASE SAVEPOINT restore_event_incidents")
                    if reset_override_cursor:
                        warnings.append(
                            "The execution-incident override cursor was reset so "
                            "future hook events cannot be skipped."
                        )
                except Exception as exc:
                    conn.execute("ROLLBACK TO SAVEPOINT restore_event_incidents")
                    conn.execute("RELEASE SAVEPOINT restore_event_incidents")
                    for name in incident_tables:
                        counts.pop(name, None)
                    warnings.append(
                        "Historical execution incidents could not be restored and "
                        f"remain in the index backup ({exc})."
                    )
            elif core_restored and available_incident_tables:
                warnings.append(
                    "Historical execution-incident state was incomplete and "
                    "remains in the index backup."
                )

        if "event_source_snippets" in source_tables:
            conn.execute("SAVEPOINT restore_event_snippets")
            try:
                conn.execute("DELETE FROM event_source_snippets")
                counts["event_source_snippets"] = _copy_attached_rows(
                    conn,
                    schema=schema,
                    table="event_source_snippets",
                    requested=_EVENT_SNIPPET_COLUMNS,
                    required=_EVENT_SNIPPET_REQUIRED_COLUMNS,
                )
                conn.execute("RELEASE SAVEPOINT restore_event_snippets")
            except Exception as exc:
                conn.execute("ROLLBACK TO SAVEPOINT restore_event_snippets")
                conn.execute("RELEASE SAVEPOINT restore_event_snippets")
                counts.pop("event_source_snippets", None)
                warnings.append(
                    "Imported event source snippets could not be restored and "
                    f"remain in the index backup ({exc})."
                )
        conn.commit()
    finally:
        try:
            conn.execute(f'DETACH DATABASE "{schema}"')
        except sqlite3.DatabaseError:
            logger.warning("Could not detach the index-recovery source", exc_info=True)

    if not core_restored:
        return counts, warnings

    # Legacy override tables may not carry write_seq. Re-running the additive
    # schema helper assigns a stable frontier after all rows have landed. When
    # override restoration failed or its exact sequence was unavailable, the
    # loop cursor above was reset so low-numbered hook writes cannot be skipped.
    # A usable ordering frontier is part of the recorder's durable state. If
    # this additive migration cannot complete, leave recovery failed (with the
    # verified backup retained) instead of reporting a ready index that could
    # silently skip later hook overrides.
    ensure_view_schema(conn)
    conn.commit()

    try:
        from clawjournal.events.search.schema import ensure_search_schema

        ensure_search_schema(conn)
    except Exception as exc:
        conn.rollback()
        warnings.append(
            "The event search index could not be rebuilt; the base events were "
            f"preserved ({exc})."
        )
    try:
        index_module.backfill_session_keys(conn)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        warnings.append(
            "Workbench-to-event links could not be refreshed; event data was "
            f"preserved ({exc})."
        )
    return counts, warnings


def _filter_rows_with_references(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    references: tuple[tuple[str, str, str], ...],
) -> tuple[list[dict[str, Any]], int]:
    """Drop damaged child rows whose rebuilt parent no longer exists."""

    parent_keys: dict[tuple[str, str], set[Any]] = {}
    for _child_column, parent_table, parent_column in references:
        key = (parent_table, parent_column)
        if key not in parent_keys:
            parent_keys[key] = {
                row[0]
                for row in conn.execute(
                    f'SELECT "{parent_column}" FROM "{parent_table}"'
                ).fetchall()
            }
    safe: list[dict[str, Any]] = []
    for row in rows:
        if all(
            row.get(child_column) in parent_keys[(parent_table, parent_column)]
            for child_column, parent_table, parent_column in references
        ):
            safe.append(row)
    return safe, len(rows) - len(safe)


def _restore_session_state(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> tuple[int, int, int]:
    restored = 0
    needs_review = 0
    skipped_share_links = 0
    share_ids = {
        row[0] for row in conn.execute("SELECT share_id FROM shares").fetchall()
    }
    for row in rows:
        session_id = row.get("session_id")
        if not session_id:
            continue
        raw_share_id = row.get("share_id")
        share_id = raw_share_id if raw_share_id in share_ids else None
        if raw_share_id is not None and share_id is None:
            skipped_share_links += 1
        current = conn.execute(
            "SELECT content_revision FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        same_revision = (
            current is not None
            and current["content_revision"] == row.get("content_revision")
        )
        if current is None:
            conn.execute(
                """INSERT INTO sessions (
                    session_id, project, source, display_title, review_status,
                    selection_reason, reviewer_notes, reviewed_at, blob_path,
                    raw_source_path, indexed_at, updated_at, share_id,
                    hold_state, embargo_until, content_revision
                ) VALUES (?, ?, ?, ?, 'new', NULL, NULL, NULL, ?, ?, ?, ?, ?,
                          'pending_review', NULL, ?)""",
                (
                    session_id,
                    row.get("project") or "recovered",
                    row.get("source") or "recovered",
                    row.get("display_title") or "Recovered trace",
                    row.get("blob_path"),
                    row.get("raw_source_path"),
                    row.get("indexed_at")
                    or datetime.now(timezone.utc).isoformat(),
                    row.get("updated_at"),
                    share_id,
                    row.get("content_revision"),
                ),
            )
            needs_review += 1
            continue
        if not same_revision:
            conn.execute(
                "UPDATE sessions SET review_status = 'new', hold_state = "
                "'pending_review', embargo_until = NULL WHERE session_id = ?",
                (session_id,),
            )
            needs_review += 1
            continue
        hold_state = row.get("hold_state")
        if hold_state not in index_module.HOLD_STATES:
            conn.execute(
                """UPDATE sessions SET
                    review_status = 'new', selection_reason = ?, reviewer_notes = ?,
                    reviewed_at = NULL, share_id = ?, hold_state = 'pending_review',
                    embargo_until = NULL
                   WHERE session_id = ?""",
                (
                    row.get("selection_reason"),
                    row.get("reviewer_notes"),
                    share_id,
                    session_id,
                ),
            )
            needs_review += 1
            continue
        conn.execute(
            """UPDATE sessions SET
                review_status = ?, selection_reason = ?, reviewer_notes = ?,
                reviewed_at = ?, share_id = ?, hold_state = ?, embargo_until = ?
               WHERE session_id = ?""",
            (
                row.get("review_status") or "new",
                row.get("selection_reason"),
                row.get("reviewer_notes"),
                row.get("reviewed_at"),
                share_id,
                hold_state,
                row.get("embargo_until"),
                session_id,
            ),
        )
        restored += 1
    return restored, needs_review, skipped_share_links


def _restore_finding_decisions(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> tuple[int, int]:
    restored = 0
    skipped = 0
    for row in rows:
        cursor = conn.execute(
            """UPDATE findings SET status = ?, decided_by = ?,
                      decision_source_id = ?, decided_at = ?, decision_reason = ?
                 WHERE session_id = ? AND engine = ? AND entity_hash = ?
                   AND revision = ?""",
            (
                row.get("status"),
                row.get("decided_by"),
                row.get("decision_source_id"),
                row.get("decided_at"),
                row.get("decision_reason"),
                row.get("session_id"),
                row.get("engine"),
                row.get("entity_hash"),
                row.get("revision"),
            ),
        )
        if cursor.rowcount:
            restored += cursor.rowcount
        else:
            skipped += 1
    return restored, skipped


def _safe_enrollment_rows(
    rows: list[dict[str, Any]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Normalize older enrollment rows, always removing active run authority."""

    safe: list[dict[str, Any]] = []
    for original in rows:
        enrollment = dict(original)
        try:
            sources = json.loads(enrollment["enrolled_sources_json"])
            projects = json.loads(enrollment["enrolled_projects_json"])
            if (
                not isinstance(sources, list)
                or not all(isinstance(item, str) and item for item in sources)
                or not isinstance(projects, list)
                or not all(isinstance(item, str) and item for item in projects)
                or not enrollment.get("enrolled_at")
                or not enrollment.get("client_enrollment_id")
            ):
                raise ValueError("invalid recurring-upload scope")
            if "enrolled_scope_entries_json" in enrollment:
                entries = json.loads(enrollment["enrolled_scope_entries_json"])
                if (
                    not isinstance(entries, list)
                    or not all(
                        isinstance(entry, list)
                        and len(entry) == 2
                        and isinstance(entry[0], str)
                        and isinstance(entry[1], str)
                        and entry[0] in sources
                        and entry[1] in projects
                        for entry in entries
                    )
                ):
                    raise ValueError("invalid recurring-upload scope entries")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            warnings.append(
                "Unreadable automatic-upload state was left disabled in the backup."
            )
            continue

        if "enrolled_scope_entries_json" not in enrollment:
            enrollment["enrolled_scope_entries_json"] = json.dumps(
                [[source, project] for source in sources for project in projects],
                separators=(",", ":"),
            )
        enrollment.setdefault("singleton_id", 1)
        generation = enrollment.get("generation")
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
        ):
            enrollment["generation"] = 1
        enrollment.setdefault("hook_targets_json", "[]")
        enrollment.setdefault("consecutive_failures", 0)
        enrollment.setdefault("revocation_pending", 0)
        enrollment.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
        if enrollment.get("mode") != "off":
            enrollment["mode"] = "paused"
            enrollment["health"] = "action_required"
            enrollment["current_run_id"] = None
            enrollment["current_run_stage"] = None
            enrollment["next_retry_at"] = None
            enrollment["last_result_code"] = "index_recovery_review_required"
            warnings.append(
                "Automatic uploads were restored paused and must be reviewed."
            )
        else:
            enrollment["health"] = "ready"
        safe.append(enrollment)
    return safe


def _restore_snapshot(
    conn: sqlite3.Connection,
    snapshot: dict[str, list[dict[str, Any]]],
    *,
    unreadable_state: list[str],
) -> tuple[dict[str, int], list[str]]:
    counts: dict[str, int] = {}
    warnings: list[str] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("SAVEPOINT restore_benchmarks")
        try:
            counts["benchmarks"] = _insert_rows(
                conn, "benchmarks", snapshot.get("benchmarks", [])
            )
            benchmark_tasks, skipped_benchmark_tasks = _filter_rows_with_references(
                conn,
                snapshot.get("benchmark_tasks", []),
                (("benchmark_id", "benchmarks", "benchmark_id"),),
            )
            counts["benchmark_tasks"] = _insert_rows(
                conn, "benchmark_tasks", benchmark_tasks
            )
            benchmark_exports, skipped_benchmark_exports = (
                _filter_rows_with_references(
                    conn,
                    snapshot.get("benchmark_exports", []),
                    (("benchmark_id", "benchmarks", "benchmark_id"),),
                )
            )
            counts["benchmark_exports"] = _insert_rows(
                conn, "benchmark_exports", benchmark_exports
            )
            skipped_benchmark_rows = (
                skipped_benchmark_tasks + skipped_benchmark_exports
            )
            if skipped_benchmark_rows:
                warnings.append(
                    f"{skipped_benchmark_rows} orphaned benchmark row(s) remain "
                    "in the backup."
                )
            conn.execute("RELEASE SAVEPOINT restore_benchmarks")
        except Exception as exc:
            conn.execute("ROLLBACK TO SAVEPOINT restore_benchmarks")
            conn.execute("RELEASE SAVEPOINT restore_benchmarks")
            for name in ("benchmarks", "benchmark_tasks", "benchmark_exports"):
                counts.pop(name, None)
            warnings.append(
                "Historical benchmark state could not be restored and remains "
                f"in the index backup ({exc})."
            )
        counts["shares"] = _insert_rows(conn, "shares", snapshot.get("shares", []))
        restored, needs_review, skipped_session_share_links = _restore_session_state(
            conn, snapshot.get("sessions", [])
        )
        counts["session_decisions"] = restored
        if needs_review:
            warnings.append(
                f"{needs_review} trace revision(s) require review before sharing."
            )
        if skipped_session_share_links:
            warnings.append(
                f"{skipped_session_share_links} orphaned session share link(s) "
                "remain in the backup."
            )
        counts["policies"] = _insert_rows(
            conn, "policies", snapshot.get("policies", [])
        )
        counts["findings_allowlist"] = _insert_rows(
            conn,
            "findings_allowlist",
            snapshot.get("findings_allowlist", []),
        )
        hold_history, skipped_history = _filter_rows_with_references(
            conn,
            snapshot.get("session_hold_history", []),
            (("session_id", "sessions", "session_id"),),
        )
        counts["hold_history"] = _insert_rows(
            conn,
            "session_hold_history",
            hold_history,
        )
        if skipped_history:
            warnings.append(
                f"{skipped_history} orphaned hold-history row(s) remain in the backup."
            )
        share_sessions, skipped_share_sessions = _filter_rows_with_references(
            conn,
            snapshot.get("share_sessions", []),
            (
                ("share_id", "shares", "share_id"),
                ("session_id", "sessions", "session_id"),
            ),
        )
        counts["share_sessions"] = _insert_rows(
            conn, "share_sessions", share_sessions
        )
        if skipped_share_sessions:
            warnings.append(
                f"{skipped_share_sessions} orphaned share link(s) remain in the backup."
            )
        finding_count, skipped_findings = _restore_finding_decisions(
            conn, snapshot.get("finding_decisions", [])
        )
        counts["finding_decisions"] = finding_count
        if skipped_findings:
            warnings.append(
                f"{skipped_findings} stale finding decision(s) were not reapplied."
            )

        enrollment_rows = _safe_enrollment_rows(
            snapshot.get("auto_upload_enrollment", []),
            warnings,
        )
        counts["auto_upload_enrollment"] = _insert_rows(
            conn, "auto_upload_enrollment", enrollment_rows
        )
        # A queued/running enrollment job must never resume from a database that
        # was rebuilt. The accepted request remains in the backup for diagnosis.
        if snapshot.get("auto_upload_enrollment_job"):
            warnings.append(
                "An unfinished automatic-upload setup was left in the backup."
            )
        benchmark_state_names = {
            "benchmarks",
            "benchmark tasks",
            "benchmark exports",
        }
        unreadable_benchmark_state = [
            name for name in unreadable_state if name in benchmark_state_names
        ]
        unreadable_safety_state = [
            name for name in unreadable_state if name not in benchmark_state_names
        ]
        if unreadable_benchmark_state:
            warnings.append(
                "Some historical benchmark state was unreadable and remains in "
                "the index backup."
            )
        if unreadable_safety_state:
            # Losing hold/share history must never silently make a rebuilt
            # trace eligible for egress. The user can review and release
            # individual traces after seeing the recovery warning.
            conn.execute(
                "UPDATE sessions SET hold_state = 'pending_review', "
                "embargo_until = NULL, review_status = 'new'"
            )
            warnings.append(
                "Some safety state was unreadable; every rebuilt trace now "
                "requires review before sharing."
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return counts, warnings


def _guided_rebuild_locked(
    scan_callback: Callable[[], dict[str, Any]],
    *,
    on_source_retired: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Back up, rebuild, rescan, and restore durable state synchronously."""

    database = _index_path()
    marker = _load_marker(database)
    backup_dir: Path | None = None
    backup_files: list[Path] = []
    _set_health({
        **current_index_health(),
        "status": "rebuilding",
        "stage": "checking_recovery",
        "message": "Checking which local decisions can be restored...",
    })

    raw_backup = marker.get("backup_path") if marker is not None else None
    if isinstance(raw_backup, str) and raw_backup:
        backup_dir, backup_files = _backup_from_marker(database, marker)
        recovery_source = backup_dir / database.name
        snapshot, errors = _snapshot_from_path(recovery_source)
        _set_health({
            **current_index_health(),
            "status": "rebuilding",
            "stage": "backing_up",
            "message": "Using the verified backup from the previous attempt...",
        })
    else:
        marker = dict(marker or {})
        marker.setdefault("started_at", datetime.now(timezone.utc).isoformat())
        marker.update({
            "version": 1,
            "database_path": str(database.resolve()),
            "stage": "preparing",
        })
        marker.pop("backup_path", None)
        marker.pop("error", None)
        # Publish the cross-process gate before reading or copying the source.
        # A crash in this stage leaves the original untouched; the same marker
        # tells a retry to create a fresh backup rather than guess.
        _write_marker(database, marker)
        recovery_source = database
        source_guard: sqlite3.Connection | None = None
        snapshot = {}
        errors = []
        try:
            try:
                source_guard = _exclusive_source_connection(database)
            except sqlite3.DatabaseError as exc:
                if _is_storage_or_lock_error(exc):
                    raise UnsafeIndexRecovery(
                        "The workbench index is still in use. Close other "
                        "ClawJournal processes and retry recovery."
                    ) from exc
                # A structurally unreadable SQLite file cannot provide a
                # transaction snapshot. Preserve its exact bytes first, then
                # make one best-effort read from that immutable backup.
                errors = ["database schema and durable safety state"]
            if source_guard is not None:
                snapshot, errors = _recovery_snapshot(source_guard)

            _set_health({
                **current_index_health(),
                "status": "rebuilding",
                "stage": "backing_up",
                "message": "Backing up the damaged index before changing anything...",
            })
            backup_dir, backup_files = _backup_index_files(database)
        finally:
            if source_guard is not None:
                try:
                    source_guard.rollback()
                finally:
                    source_guard.close()

        recovery_source = backup_dir / database.name
        if not snapshot and errors == ["database schema and durable safety state"]:
            snapshot, errors = _snapshot_from_path(recovery_source)
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database_path": str(database.resolve()),
            "files": [path.name for path in backup_files],
            "unreadable_state": errors,
            "recoverable_state_counts": {
                name: len(rows) for name, rows in snapshot.items()
            },
        }
        manifest_path = backup_dir / "recovery-manifest.json"
        with manifest_path.open("w", encoding="utf-8") as manifest_file:
            json.dump(manifest, manifest_file, indent=2, sort_keys=True)
            manifest_file.flush()
            os.fsync(manifest_file.fileno())
        _fsync_directory(backup_dir)
    marker.update({
        "version": 1,
        "database_path": str(database.resolve()),
        "backup_path": str(backup_dir),
        "stage": "backed_up",
    })
    marker.pop("error", None)
    _write_marker(database, marker)

    previous_recovery_access = index_module._set_index_recovery_access(True)
    try:
        _remove_index_files(database)
        if on_source_retired is not None:
            try:
                on_source_retired()
            except Exception:
                logger.exception(
                    "Could not resume workbench request admission after retiring "
                    "the damaged index"
                )
        marker["stage"] = "reindexing"
        _write_marker(database, marker)
        _set_health({
            **current_index_health(),
            "status": "rebuilding",
            "stage": "reindexing",
            "message": "Rebuilding the local index from the original session logs...",
            "backup_path": str(backup_dir),
        })
        fresh = index_module.open_index()
        fresh.close()
        scan_report = scan_callback()
        if scan_report.get("ok") is not True:
            raise RuntimeError("The source-log rescan did not complete cleanly.")

        marker["stage"] = "restoring_state"
        _write_marker(database, marker)
        _set_health({
            **current_index_health(),
            "status": "rebuilding",
            "stage": "restoring_state",
            "message": "Restoring review, hold, sharing, and policy state...",
        })
        rebuilt = index_module.open_index()
        try:
            event_counts, event_warnings = _restore_execution_recorder(
                rebuilt,
                recovery_source,
            )
            restored_counts, warnings = _restore_snapshot(
                rebuilt,
                snapshot,
                unreadable_state=errors,
            )
            restored_counts.update(event_counts)
            warnings = event_warnings + warnings
            index_module.backfill_session_keys(rebuilt)
            rebuilt.commit()
            check = [str(row[0]) for row in rebuilt.execute("PRAGMA quick_check(1)")]
            if check != ["ok"]:
                raise sqlite3.DatabaseError(check[0] if check else "integrity check failed")
            foreign_key_errors = rebuilt.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise sqlite3.DatabaseError(
                    "The rebuilt index failed its foreign-key integrity check."
                )
        finally:
            rebuilt.close()
    except Exception as exc:
        logger.exception("Guided workbench-index recovery failed")
        marker["stage"] = "failed"
        marker["error"] = str(exc)
        _write_marker(database, marker)
        failed = {
            "database_path": str(database.resolve()),
            "sqlite_version": sqlite3.sqlite_version,
            "journal_mode": index_module.INDEX_JOURNAL_MODE,
            "status": "recovery_required",
            "message": (
                "Recovery did not finish. The original index backup is intact "
                "and the partial rebuild will not be used."
            ),
            "detail": str(exc),
            "backup_path": str(backup_dir),
            "automatic_recovery_available": True,
            "interrupted_recovery": True,
        }
        _set_health(failed)
        raise
    finally:
        index_module._set_index_recovery_access(previous_recovery_access)

    _marker_path(database).unlink(missing_ok=True)
    _fsync_directory(database.parent)
    ready = {
        "status": "ready",
        "message": "The workbench index was rebuilt and verified.",
        "database_path": str(database.resolve()),
        "sqlite_version": sqlite3.sqlite_version,
        "journal_mode": index_module.INDEX_JOURNAL_MODE,
        "automatic_recovery_available": False,
        "backup_path": str(backup_dir),
        "restored_state_counts": restored_counts,
        "warnings": warnings,
    }
    return _set_health(ready)


def guided_rebuild(
    scan_callback: Callable[[], dict[str, Any]],
    *,
    on_source_retired: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Rebuild only after every guarded cross-process connection has closed."""

    database = _index_path()
    marker = _load_marker(database)
    if marker is None:
        marker = {
            "version": 1,
            "database_path": str(database.resolve()),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "stage": "preparing",
        }
        _write_marker(database, marker)
    lease = index_module._exclusive_index_recovery_lease(timeout=30.0)
    try:
        lease.__enter__()
    except sqlite3.OperationalError as exc:
        failed = {
            **current_index_health(),
            "status": "recovery_required",
            "stage": "waiting_for_connections",
            "message": (
                "Close other ClawJournal processes that are using the index, "
                "then retry recovery. The original index was not changed."
            ),
            "detail": str(exc),
            "automatic_recovery_available": True,
            "interrupted_recovery": True,
        }
        _set_health(failed)
        raise UnsafeIndexRecovery(failed["message"]) from exc
    try:
        return _guided_rebuild_locked(
            scan_callback,
            on_source_retired=on_source_retired,
        )
    finally:
        lease.__exit__(None, None, None)
