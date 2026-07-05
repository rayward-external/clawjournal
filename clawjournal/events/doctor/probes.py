"""Doctor probes — read state, never write (phase-1 plan 08).

All DB queries run inside one read-only SQLite transaction so the
report is internally consistent against a single snapshot, even when
``clawjournal serve`` is concurrently writing.

Filesystem fallback uses the constants in ``clawjournal/parsing/parser.py``
when ``event_sessions`` hasn't been populated yet — no new path
discovery in this module.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clawjournal.events.capabilities import effective_matrix
from clawjournal.events.export.bundle import (
    BUNDLE_SCHEMA_VERSION,
    RECORDER_SCHEMA_VERSION,
)
from clawjournal.events.types import EVENT_TYPE_SET
from clawjournal.parsing import parser as parser_mod
from clawjournal.redaction import trufflehog as th

# Install-state branches (per plan 08 §Install-state probe)
INSTALL_FRESH = "fresh"
INSTALL_WORKBENCH_ONLY = "workbench-only"
INSTALL_EVENTS_EMPTY = "events-empty"
INSTALL_DB_MISSING = "db-missing"
INSTALL_DB_CORRUPT = "db-corrupt"
INSTALL_HEALTHY = "healthy"

# Verdicts (per plan 08 §Verdict definitions)
VERDICT_COMPATIBLE = "compatible"
VERDICT_PARTIAL = "partially-compatible"
VERDICT_UNKNOWN_SCHEMA = "unknown-schema"

# Filesystem probe table — (client_name, parser-module attr name).
# Aider is intentionally absent: its history is per-repo, not per-user
# (.aider.chat.history.md inside each working tree), so install-time
# probing makes no sense.
_FS_PROBES: tuple[tuple[str, str], ...] = (
    ("claude", "CLAUDE_DIR"),
    ("claude-science", "CLAUDE_SCIENCE_DIR"),
    ("codex", "CODEX_DIR"),
    ("openclaw", "OPENCLAW_DIR"),
    ("gemini", "GEMINI_DIR"),
    ("opencode", "OPENCODE_DIR"),
    ("kimi", "KIMI_DIR"),
    ("cursor", "CURSOR_DIR"),
    ("copilot", "COPILOT_DIR"),
)


@dataclass
class TruffleHogStatus:
    state: str  # "present" | "missing" | "unparseable-version"
    version: str | None
    # Pinned version this clawjournal expects, set only when the resolved
    # binary is the managed copy AND it drifted off the pin (additive field;
    # None for PATH binaries and pin-matched managed copies).
    off_pin_expected: str | None = None


@dataclass
class CostHealth:
    token_usage_rows: int
    cost_anomalies_rows: int
    last_event_id: int | None
    last_event_at: str | None


@dataclass
class IncidentHealth:
    counts_by_kind: dict[str, int]
    last_event_id: int | None


@dataclass
class ClientObservation:
    client: str
    client_version: str
    sessions_count: int
    event_types_observed: list[str]
    unknown_event_types: list[str]
    unsupported_event_types: list[str]
    schema_unknown_rows: int
    matrix_supported_count: int
    verdict: str


@dataclass
class DoctorReport:
    install_state: str
    install_hint: str
    clawjournal_version: str
    bundle_schema_version: str
    recorder_schema_version: str
    security_schema_version: int | None
    config_dir: str
    index_db_path: str
    events_count: int
    sessions_count: int
    trufflehog: TruffleHogStatus
    clients: list[ClientObservation]
    fs_clients: list[str] = field(default_factory=list)
    cost: CostHealth | None = None
    incidents: IncidentHealth | None = None
    warnings: list[dict[str, str]] = field(default_factory=list)


def config_dir() -> Path:
    return Path.home() / ".clawjournal"


def index_db_path() -> Path:
    return config_dir() / "index.db"


def _read_security_schema_version() -> int | None:
    try:
        from clawjournal.workbench.index import SECURITY_SCHEMA_VERSION
    except Exception:
        return None
    if isinstance(SECURITY_SCHEMA_VERSION, int):
        return SECURITY_SCHEMA_VERSION
    return None


def _read_clawjournal_version() -> str:
    try:
        return importlib.metadata.version("clawjournal")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _trufflehog_status() -> TruffleHogStatus:
    if not th.is_available():
        return TruffleHogStatus(state="missing", version=None)
    fingerprint = th.engine_fingerprint()
    off_pin = th.managed_off_pin()
    off_pin_expected = off_pin[1] if off_pin else None
    if fingerprint.startswith("trufflehog "):
        return TruffleHogStatus(
            state="present",
            version=fingerprint.split(" ", 1)[1],
            off_pin_expected=off_pin_expected,
        )
    return TruffleHogStatus(
        state="unparseable-version",
        version=fingerprint,
        off_pin_expected=off_pin_expected,
    )


def _detect_home_warning() -> dict[str, str] | None:
    home = os.path.expanduser("~")
    if home == "~" or not home or os.path.basename(home) in ("", "~"):
        return {
            "kind": "home_not_set",
            "message": (
                "HOME not set — anonymizer is a no-op; error messages will "
                "include local paths verbatim"
            ),
        }
    return None


def _filesystem_clients() -> list[str]:
    found: list[str] = []
    for client_name, attr in _FS_PROBES:
        path = getattr(parser_mod, attr, None)
        if path is None:
            continue
        try:
            if Path(path).exists():
                found.append(client_name)
        except OSError:
            continue
    return found


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0


def _classify_install_state(
    conn: sqlite3.Connection | None,
    *,
    cfg_exists: bool,
    db_exists: bool,
) -> tuple[str, str]:
    if not cfg_exists:
        return (
            INSTALL_FRESH,
            "no clawjournal state yet — run `clawjournal scan` then "
            "`clawjournal events ingest`",
        )
    if not db_exists:
        return (
            INSTALL_DB_MISSING,
            "config dir exists but index.db is missing — run `clawjournal scan`",
        )
    if conn is None:
        return (
            INSTALL_DB_CORRUPT,
            "index.db is unreadable (truncated or schema mismatch) — run "
            "`clawjournal scan --rebuild` or move the file aside",
        )
    has_workbench = _table_exists(conn, "sessions")
    has_events = _table_exists(conn, "event_sessions")
    if not has_workbench and not has_events:
        # Valid SQLite file but neither schema present — empty file
        # produced by a partial / interrupted scan, or a stray DB at
        # this path. Treat as corrupt; not a healthy "no data yet"
        # state.
        return (
            INSTALL_DB_CORRUPT,
            "index.db has no clawjournal schema (workbench or events) — run "
            "`clawjournal scan --rebuild` or move the file aside",
        )
    if has_workbench and not has_events:
        return (
            INSTALL_WORKBENCH_ONLY,
            "events schema not present — run `clawjournal events ingest` to "
            "create the events tables",
        )
    if has_events and _row_count(conn, "event_sessions") == 0:
        return (
            INSTALL_EVENTS_EMPTY,
            "ingest hasn't seen new vendor data; everything looks healthy",
        )
    return (INSTALL_HEALTHY, "events ingested and ready")


def _open_readonly(path: Path) -> sqlite3.Connection | None:
    try:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        # Single read transaction snapshot — every query runs against
        # one consistent view even when serve is writing.
        conn.execute("BEGIN")
        # A non-SQLite file opens cleanly but fails on schema reads;
        # `sqlite_master` is the cheapest read that will surface that.
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        return conn
    except sqlite3.OperationalError:
        return None
    except sqlite3.DatabaseError:
        return None


def _close_readonly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("COMMIT")
    except sqlite3.OperationalError:
        pass
    conn.close()


def _collect_clients(
    conn: sqlite3.Connection, matrix: dict[tuple[str, str], tuple[bool, str]]
) -> list[ClientObservation]:
    rows = list(
        conn.execute(
            "SELECT client, COALESCE(client_version, '') AS client_version, "
            "COUNT(*) AS sessions_count "
            "FROM event_sessions "
            "GROUP BY client, client_version "
            "ORDER BY client, client_version"
        )
    )
    if not rows:
        return []

    observations: list[ClientObservation] = []
    for row in rows:
        client = row["client"]
        version = row["client_version"]
        sessions_count = int(row["sessions_count"])

        type_rows = list(
            conn.execute(
                "SELECT DISTINCT e.type "
                "FROM events e "
                "JOIN event_sessions s ON e.session_id = s.id "
                "WHERE s.client = ? AND COALESCE(s.client_version, '') = ?",
                (client, version),
            )
        )
        event_types_observed = sorted(
            {r["type"] for r in type_rows if r["type"] is not None}
        )
        unknown_event_types = sorted(
            t for t in event_types_observed if t not in EVENT_TYPE_SET
        )

        schema_unknown_rows = int(
            conn.execute(
                "SELECT COUNT(*) FROM events e "
                "JOIN event_sessions s ON e.session_id = s.id "
                "WHERE s.client = ? AND COALESCE(s.client_version, '') = ? "
                "AND e.type = 'schema_unknown'",
                (client, version),
            ).fetchone()[0]
        )

        matrix_supported = sum(
            1
            for (mc, _et), (sup, _r) in matrix.items()
            if mc == client and sup
        )

        unsupported_event_types = sorted(
            et
            for et in event_types_observed
            if et not in unknown_event_types
            and et != "schema_unknown"
            and not matrix.get((client, et), (False, ""))[0]
        )

        verdict = _verdict(
            client,
            event_types_observed,
            unknown_event_types,
            schema_unknown_rows,
            matrix,
        )
        observations.append(
            ClientObservation(
                client=client,
                client_version=version,
                sessions_count=sessions_count,
                event_types_observed=event_types_observed,
                unknown_event_types=unknown_event_types,
                unsupported_event_types=unsupported_event_types,
                schema_unknown_rows=schema_unknown_rows,
                matrix_supported_count=matrix_supported,
                verdict=verdict,
            )
        )
    return observations


def _verdict(
    client: str,
    event_types_observed: list[str],
    unknown_event_types: list[str],
    schema_unknown_rows: int,
    matrix: dict[tuple[str, str], tuple[bool, str]],
) -> str:
    if unknown_event_types:
        return VERDICT_UNKNOWN_SCHEMA
    if schema_unknown_rows > 0:
        return VERDICT_PARTIAL
    for event_type in event_types_observed:
        supported, _ = matrix.get((client, event_type), (False, ""))
        if not supported:
            return VERDICT_PARTIAL
    return VERDICT_COMPATIBLE


def _cost_health(conn: sqlite3.Connection) -> CostHealth | None:
    if not _table_exists(conn, "token_usage"):
        return None
    token_rows = _row_count(conn, "token_usage")
    anomaly_rows = _row_count(conn, "cost_anomalies")
    last_event_id: int | None = None
    last_event_at: str | None = None
    if _table_exists(conn, "cost_ingest_state"):
        cur = conn.execute(
            "SELECT MAX(last_event_id) FROM cost_ingest_state"
        ).fetchone()
        if cur and cur[0] is not None:
            last_event_id = int(cur[0])
            ts = conn.execute(
                "SELECT event_at FROM events WHERE id = ?",
                (last_event_id,),
            ).fetchone()
            if ts and ts[0]:
                last_event_at = ts[0]
    return CostHealth(
        token_usage_rows=token_rows,
        cost_anomalies_rows=anomaly_rows,
        last_event_id=last_event_id,
        last_event_at=last_event_at,
    )


def _incident_health(conn: sqlite3.Connection) -> IncidentHealth | None:
    if not _table_exists(conn, "incidents"):
        return None
    counts: dict[str, int] = {}
    for row in conn.execute("SELECT kind, COUNT(*) FROM incidents GROUP BY kind"):
        counts[row[0]] = int(row[1])
    last_event_id: int | None = None
    if _table_exists(conn, "loop_ingest_state"):
        cur = conn.execute(
            "SELECT MAX(last_event_id) FROM loop_ingest_state"
        ).fetchone()
        if cur and cur[0] is not None:
            last_event_id = int(cur[0])
    return IncidentHealth(counts_by_kind=counts, last_event_id=last_event_id)


def collect() -> DoctorReport:
    """Run all probes and return a DoctorReport.

    Opens one read-only SQLite connection and runs all queries inside a
    single transaction. Falls back gracefully when the DB is missing or
    corrupt; falls back to filesystem probes when ``event_sessions`` is
    empty.
    """

    cfg_dir = config_dir()
    db_path = index_db_path()
    cfg_exists = cfg_dir.exists()
    db_exists = db_path.exists()

    warnings_list: list[dict[str, str]] = []
    home_warning = _detect_home_warning()
    if home_warning is not None:
        warnings_list.append(home_warning)

    matrix = effective_matrix()

    conn: sqlite3.Connection | None = None
    if cfg_exists and db_exists:
        conn = _open_readonly(db_path)

    install_state, install_hint = _classify_install_state(
        conn, cfg_exists=cfg_exists, db_exists=db_exists
    )

    events_count = 0
    sessions_count = 0
    clients: list[ClientObservation] = []
    cost: CostHealth | None = None
    incidents: IncidentHealth | None = None
    fs_clients: list[str] = []

    try:
        if conn is not None:
            if _table_exists(conn, "events"):
                events_count = _row_count(conn, "events")
            if _table_exists(conn, "event_sessions"):
                sessions_count = _row_count(conn, "event_sessions")
                if sessions_count > 0:
                    clients = _collect_clients(conn, matrix)
            cost = _cost_health(conn)
            incidents = _incident_health(conn)
        if not clients:
            fs_clients = _filesystem_clients()
    finally:
        if conn is not None:
            _close_readonly(conn)

    return DoctorReport(
        install_state=install_state,
        install_hint=install_hint,
        clawjournal_version=_read_clawjournal_version(),
        bundle_schema_version=BUNDLE_SCHEMA_VERSION,
        recorder_schema_version=RECORDER_SCHEMA_VERSION,
        security_schema_version=_read_security_schema_version(),
        config_dir=str(cfg_dir),
        index_db_path=str(db_path),
        events_count=events_count,
        sessions_count=sessions_count,
        trufflehog=_trufflehog_status(),
        clients=clients,
        fs_clients=fs_clients,
        cost=cost,
        incidents=incidents,
        warnings=warnings_list,
    )


def report_to_dict(report: DoctorReport) -> dict[str, Any]:
    """Serialize ``DoctorReport`` to JSON-safe dict (used by ``--json``)."""

    return {
        "events_doctor_schema_version": "1.0",
        "install_state": report.install_state,
        "install_hint": report.install_hint,
        "clawjournal_version": report.clawjournal_version,
        "bundle_schema_version": report.bundle_schema_version,
        "recorder_schema_version": report.recorder_schema_version,
        "security_schema_version": report.security_schema_version,
        "config_dir": report.config_dir,
        "index_db_path": report.index_db_path,
        "events_count": report.events_count,
        "sessions_count": report.sessions_count,
        "trufflehog": {
            "state": report.trufflehog.state,
            "version": report.trufflehog.version,
        },
        "clients": [
            {
                "client": c.client,
                "client_version": c.client_version,
                "sessions_count": c.sessions_count,
                "event_types_observed": list(c.event_types_observed),
                "unknown_event_types": list(c.unknown_event_types),
                "unsupported_event_types": list(c.unsupported_event_types),
                "schema_unknown_rows": c.schema_unknown_rows,
                "matrix_supported_count": c.matrix_supported_count,
                "verdict": c.verdict,
            }
            for c in report.clients
        ],
        "fs_clients": list(report.fs_clients),
        "cost": (
            None
            if report.cost is None
            else {
                "token_usage_rows": report.cost.token_usage_rows,
                "cost_anomalies_rows": report.cost.cost_anomalies_rows,
                "last_event_id": report.cost.last_event_id,
                "last_event_at": report.cost.last_event_at,
            }
        ),
        "incidents": (
            None
            if report.incidents is None
            else {
                "counts_by_kind": dict(report.incidents.counts_by_kind),
                "last_event_id": report.incidents.last_event_id,
            }
        ),
        "warnings": [dict(w) for w in report.warnings],
    }


def exit_code_for(report: DoctorReport) -> int:
    """Map the collected report to the plan's exit-code schedule."""

    if report.install_state == INSTALL_DB_MISSING:
        return 3
    if report.install_state == INSTALL_DB_CORRUPT:
        return 5
    if report.install_state == INSTALL_WORKBENCH_ONLY:
        return 1
    # FRESH and EVENTS_EMPTY are healthy zero-data states.
    for client in report.clients:
        if client.verdict == VERDICT_UNKNOWN_SCHEMA:
            return 6
    for client in report.clients:
        if client.verdict == VERDICT_PARTIAL:
            return 1
    return 0


__all__ = [
    "ClientObservation",
    "CostHealth",
    "DoctorReport",
    "INSTALL_DB_CORRUPT",
    "INSTALL_DB_MISSING",
    "INSTALL_EVENTS_EMPTY",
    "INSTALL_FRESH",
    "INSTALL_HEALTHY",
    "INSTALL_WORKBENCH_ONLY",
    "IncidentHealth",
    "TruffleHogStatus",
    "VERDICT_COMPATIBLE",
    "VERDICT_PARTIAL",
    "VERDICT_UNKNOWN_SCHEMA",
    "collect",
    "config_dir",
    "exit_code_for",
    "index_db_path",
    "report_to_dict",
]
