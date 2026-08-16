"""Privacy-bounded diagnostics for support reports.

This module deliberately does not call :func:`workbench.index.open_index`:
doing so would create or migrate the database, acquire ClawJournal's normal
state files, and turn a diagnostic command into a write path.  The index probe
opens only an already-existing SQLite file in read-only mode and reports a
small, schema-versioned allowlist of values.
"""

from __future__ import annotations

import platform
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Mapping

from . import __version__


SUPPORT_DIAGNOSTICS_SCHEMA_VERSION = 1
MAX_QUICK_CHECK_ISSUES = 20
MAX_FOREIGN_KEY_VIOLATIONS = 20
_REVISION_RE = re.compile(r"[0-9a-fA-F]{7,64}")
_KNOWN_SCHEMA_IDENTIFIERS = frozenset({
    "auto_upload_enrollment",
    "auto_upload_enrollment_job",
    "benchmark_exports",
    "benchmark_tasks",
    "benchmarks",
    "capture_cursors",
    "cost_anomalies",
    "cost_ingest_state",
    "event_overrides",
    "event_sessions",
    "event_source_snippets",
    "events",
    "findings",
    "findings_allowlist",
    "incidents",
    "loop_ingest_state",
    "policies",
    "session_hold_history",
    "sessions",
    "share_sessions",
    "shares",
    "token_usage",
})
_OS_FAMILY_ALIASES = {
    "darwin": "macOS",
    "freebsd": "FreeBSD",
    "linux": "Linux",
    "windows": "Windows",
}
_ARCHITECTURE_ALIASES = {
    "aarch64": "arm64",
    "amd64": "x86_64",
    "arm64": "arm64",
    "i386": "x86",
    "i686": "x86",
    "ppc64le": "ppc64le",
    "riscv64": "riscv64",
    "s390x": "s390x",
    "x86": "x86",
    "x86_64": "x86_64",
}
_OS_RELEASE_PREFIX_RE = re.compile(r"[0-9]+(?:\.[0-9]+){0,3}")
_JOURNAL_MODES = frozenset({"delete", "truncate", "persist", "memory", "wal", "off"})


def _checkout_root() -> Path | None:
    """Return the checkout containing the imported package, when present."""

    root = Path(__file__).resolve().parent.parent
    return root if (root / ".git").exists() else None


def package_revision() -> str | None:
    """Return a validated full HEAD revision without contacting a remote."""

    root = _checkout_root()
    if root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    revision = result.stdout.decode("ascii", "replace").strip()
    return revision.lower() if _REVISION_RE.fullmatch(revision) else None


def version_string() -> str:
    """Return the human CLI version, including checkout HEAD when available."""

    revision = package_revision()
    suffix = f" ({revision[:7]})" if revision else ""
    return f"clawjournal {__version__}{suffix}"


def _expected_schema_version() -> int | None:
    """Read the schema sentinel without opening or initializing the index."""

    try:
        from .workbench.index import WORKBENCH_SCHEMA_VERSION
    except Exception:  # A partial install must still produce diagnostics.
        return None
    return (
        WORKBENCH_SCHEMA_VERSION
        if isinstance(WORKBENCH_SCHEMA_VERSION, int)
        else None
    )


def _storage_report(state_dir: Path) -> dict[str, object]:
    """Storage-inspection integration seam.

    The storage guard owns platform-specific mount discovery.  Keeping this
    adapter narrow lets ``doctor`` degrade safely on partial/older installs and
    ensures no mount source or absolute path enters the support payload.
    """

    try:
        from .filesystem import classify_filesystem, sanitized_filesystem_type
    except (ImportError, AttributeError):
        return {
            "filesystem_type": "unknown",
            "storage_risk": "unknown",
            "storage_migration_required": False,
        }

    try:
        raw = classify_filesystem(state_dir)
    except Exception:
        raw = None

    if isinstance(raw, Mapping):
        filesystem_type = raw.get("filesystem_type")
        storage_risk = raw.get("storage_risk")
        migration_required = raw.get("storage_migration_required")
    else:
        filesystem_type = getattr(raw, "filesystem_type", None)
        storage_risk = getattr(raw, "storage_risk", None)
        migration_required = getattr(raw, "storage_migration_required", None)

    normalized_type = sanitized_filesystem_type(filesystem_type)
    normalized_risk = str(storage_risk or "unknown").strip().lower()
    if normalized_risk not in {"network", "local", "unknown"}:
        normalized_risk = "unknown"
    return {
        "filesystem_type": normalized_type,
        "storage_risk": normalized_risk,
        "storage_migration_required": migration_required is True,
    }


def _empty_quick_check(status: str = "not_run") -> dict[str, object]:
    return {"status": status, "issue_count": 0, "truncated": False}


def _empty_foreign_key_check(status: str = "not_run") -> dict[str, object]:
    return {
        "status": status,
        "returned_count": 0,
        "truncated": False,
        "violations": [],
    }


def _safe_schema_identifier(value: object) -> str:
    text = str(value) if value is not None else ""
    return text if text in _KNOWN_SCHEMA_IDENTIFIERS else "redacted"


def _safe_os_family(value: object) -> str:
    return _OS_FAMILY_ALIASES.get(str(value or "").strip().lower(), "unknown")


def _safe_os_release(value: object) -> str:
    """Return only the numeric OS/kernel prefix, never a custom build suffix."""

    match = _OS_RELEASE_PREFIX_RE.match(str(value or "").strip())
    return match.group(0) if match else "unknown"


def _safe_architecture(value: object) -> str:
    return _ARCHITECTURE_ALIASES.get(
        str(value or "").strip().lower(),
        "unknown",
    )


def _classify_open_error(exc: BaseException) -> str:
    """Map SQLite/OS messages to fixed codes; never return their raw text."""

    message = str(exc).lower()
    if "locked" in message or "busy" in message:
        return "locked"
    if "not a database" in message or "malformed" in message or "corrupt" in message:
        return "corrupt"
    if "permission" in message or "access is denied" in message:
        return "permission_denied"
    return "unreadable"


def _header_journal_mode(database: Path) -> str | None:
    """Read the SQLite header's persistent rollback/WAL format marker."""

    try:
        with database.open("rb") as file:
            header = file.read(20)
    except OSError:
        return None
    if len(header) < 20 or header[:16] != b"SQLite format 3\x00":
        return None
    write_version, read_version = header[18], header[19]
    if 2 in {write_version, read_version}:
        return "wal"
    if write_version == read_version == 1:
        return "delete"
    return None


def _connect_readonly(database: Path) -> sqlite3.Connection:
    # SQLite's ordinary mode=ro can still create shared-memory/WAL sidecars.
    # immutable=1 guarantees this evidence probe never writes beside the DB.
    # Callers decline to open databases with existing sidecars because an
    # immutable main-file view would omit their uncheckpointed state.
    uri = database.resolve().as_uri() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=1.0, isolation_level=None)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA busy_timeout=1000")
    return conn


def _read_scalar_pragma(conn: sqlite3.Connection, pragma: str) -> object | None:
    try:
        row = conn.execute(pragma).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _run_quick_check(conn: sqlite3.Connection) -> dict[str, object]:
    try:
        rows = conn.execute(
            f"PRAGMA quick_check({MAX_QUICK_CHECK_ISSUES})"
        ).fetchmany(MAX_QUICK_CHECK_ISSUES)
    except sqlite3.Error as exc:
        error_code = _classify_open_error(exc)
        return _empty_quick_check(
            error_code if error_code in {"locked", "corrupt"} else "unavailable"
        )

    results = [str(row[0]) for row in rows if row]
    if results == ["ok"]:
        return _empty_quick_check("ok")
    return {
        "status": "failed",
        # Raw integrity messages can contain schema identifiers controlled by
        # a damaged database.  A support report needs only the bounded count.
        "issue_count": len(results),
        "truncated": len(results) >= MAX_QUICK_CHECK_ISSUES,
    }


def _run_foreign_key_check(conn: sqlite3.Connection) -> dict[str, object]:
    try:
        rows = conn.execute("PRAGMA foreign_key_check").fetchmany(
            MAX_FOREIGN_KEY_VIOLATIONS + 1
        )
    except sqlite3.Error as exc:
        error_code = _classify_open_error(exc)
        return _empty_foreign_key_check(
            error_code if error_code in {"locked", "corrupt"} else "unavailable"
        )

    truncated = len(rows) > MAX_FOREIGN_KEY_VIOLATIONS
    rows = rows[:MAX_FOREIGN_KEY_VIOLATIONS]
    violations: list[dict[str, object]] = []
    for row in rows:
        row_id = row[1] if len(row) > 1 and isinstance(row[1], int) else None
        foreign_key_id = row[3] if len(row) > 3 and isinstance(row[3], int) else None
        violations.append(
            {
                "table": _safe_schema_identifier(row[0] if len(row) > 0 else None),
                "row_id": row_id,
                "parent": _safe_schema_identifier(row[2] if len(row) > 2 else None),
                "foreign_key_id": foreign_key_id,
            }
        )
    return {
        "status": "violations" if violations else "ok",
        "returned_count": len(violations),
        "truncated": truncated,
        "violations": violations,
    }


def _index_report(database: Path, expected_schema: int | None) -> dict[str, object]:
    base: dict[str, object] = {
        "exists": False,
        "health_code": "missing",
        "user_version": None,
        "journal_mode": None,
        "quick_check": _empty_quick_check(),
        "foreign_key_check": _empty_foreign_key_check(),
    }
    try:
        exists = database.is_file()
    except OSError:
        base["exists"] = None
        base["health_code"] = "unreadable"
        return base
    if not exists:
        return base

    base["exists"] = True
    header_journal_mode = _header_journal_mode(database)
    try:
        live_sidecars = [
            suffix
            for suffix in ("-wal", "-shm", "-journal")
            if Path(str(database) + suffix).is_file()
        ]
    except OSError:
        base["health_code"] = "unreadable"
        return base
    if live_sidecars:
        # Opening an immutable main file would silently ignore WAL contents;
        # an ordinary read-only open can create/modify sidecars. Report the
        # bounded condition and leave an offline-copy inspection to the user.
        base["health_code"] = "sidecar_snapshot_not_inspected"
        base["journal_mode"] = (
            "wal"
            if "-wal" in live_sidecars or header_journal_mode == "wal"
            else None
        )
        return base
    try:
        conn = _connect_readonly(database)
    except (OSError, sqlite3.Error) as exc:
        base["health_code"] = _classify_open_error(exc)
        return base

    try:
        raw_user_version = _read_scalar_pragma(conn, "PRAGMA user_version")
        base["user_version"] = (
            raw_user_version if isinstance(raw_user_version, int) else None
        )
        raw_journal_mode = _read_scalar_pragma(conn, "PRAGMA journal_mode")
        journal_mode = header_journal_mode or str(
            raw_journal_mode or ""
        ).strip().lower()
        base["journal_mode"] = journal_mode if journal_mode in _JOURNAL_MODES else None
        quick_check = _run_quick_check(conn)
        foreign_key_check = _run_foreign_key_check(conn)
        base["quick_check"] = quick_check
        base["foreign_key_check"] = foreign_key_check
    finally:
        conn.close()

    if "corrupt" in {quick_check["status"], foreign_key_check["status"]}:
        base["health_code"] = "corrupt"
    elif "locked" in {quick_check["status"], foreign_key_check["status"]}:
        base["health_code"] = "locked"
    elif quick_check["status"] == "failed":
        base["health_code"] = "integrity_check_failed"
    elif foreign_key_check["status"] == "violations":
        base["health_code"] = "foreign_key_violations"
    elif quick_check["status"] != "ok" or foreign_key_check["status"] != "ok":
        base["health_code"] = "inspection_partial"
    elif expected_schema is None or base["user_version"] is None:
        base["health_code"] = "schema_unknown"
    elif base["user_version"] < expected_schema:
        base["health_code"] = "schema_outdated"
    elif base["user_version"] > expected_schema:
        base["health_code"] = "schema_newer"
    elif base["journal_mode"] != "delete":
        base["health_code"] = "unexpected_journal_mode"
    else:
        base["health_code"] = "healthy"
    return base


def collect_index_diagnostics(*, state_dir: Path | None = None) -> dict[str, object]:
    """Collect a read-only, privacy-bounded diagnostic payload."""

    if state_dir is None:
        # Resolve dynamically so embedders/tests can relocate the coupled state
        # root without this module retaining an import-time path.
        from . import config

        state_dir = config.CONFIG_DIR
    state_dir = Path(state_dir)
    expected_schema = _expected_schema_version()
    revision = package_revision()
    storage = _storage_report(state_dir)
    if storage.get("storage_migration_required") is True:
        # Do not stat or open a database once the mount table already proves
        # that it is network-backed. This keeps doctor responsive on a hard,
        # disconnected NFS mount and avoids SQLite side effects there.
        index = {
            "exists": None,
            "health_code": "network_storage_not_inspected",
            "user_version": None,
            "journal_mode": None,
            "quick_check": _empty_quick_check(),
            "foreign_key_check": _empty_foreign_key_check(),
        }
    else:
        index = _index_report(state_dir / "index.db", expected_schema)
    return {
        "support_diagnostics_schema_version": SUPPORT_DIAGNOSTICS_SCHEMA_VERSION,
        "kind": "index",
        "package": {"version": __version__, "revision": revision},
        "runtime": {
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "os_family": _safe_os_family(platform.system()),
            "os_release": _safe_os_release(platform.release()),
            "architecture": _safe_architecture(platform.machine()),
        },
        "schema": {"expected_user_version": expected_schema},
        "storage": storage,
        "index": index,
    }


def diagnostics_exit_code(report: Mapping[str, object]) -> int:
    storage = report.get("storage")
    index = report.get("index")
    migration_required = (
        isinstance(storage, Mapping)
        and storage.get("storage_migration_required") is True
    )
    health_code = index.get("health_code") if isinstance(index, Mapping) else None
    return 0 if health_code == "healthy" and not migration_required else 1


def render_index_diagnostics(report: Mapping[str, object]) -> str:
    """Render the same allowlisted values without exposing the state path."""

    package = report.get("package") if isinstance(report.get("package"), Mapping) else {}
    runtime = report.get("runtime") if isinstance(report.get("runtime"), Mapping) else {}
    schema = report.get("schema") if isinstance(report.get("schema"), Mapping) else {}
    storage = report.get("storage") if isinstance(report.get("storage"), Mapping) else {}
    index = report.get("index") if isinstance(report.get("index"), Mapping) else {}
    revision = package.get("revision")
    version = str(package.get("version") or "unknown")
    if isinstance(revision, str) and _REVISION_RE.fullmatch(revision):
        version += f" ({revision[:7]})"
    migration = "required" if storage.get("storage_migration_required") is True else "not required"
    return "\n".join(
        [
            f"ClawJournal index diagnostics: {index.get('health_code') or 'unknown'}",
            f"Version: {version}",
            (
                "Runtime: Python "
                f"{runtime.get('python_version') or 'unknown'}; SQLite "
                f"{runtime.get('sqlite_version') or 'unknown'}; "
                f"{runtime.get('os_family') or 'unknown'} "
                f"{runtime.get('os_release') or 'unknown'} "
                f"({runtime.get('architecture') or 'unknown'})"
            ),
            (
                f"Storage: {storage.get('filesystem_type') or 'unknown'} "
                f"({storage.get('storage_risk') or 'unknown'}; migration {migration})"
            ),
            (
                f"Index: exists={index.get('exists')}; "
                f"schema={index.get('user_version')}/"
                f"{schema.get('expected_user_version')}; "
                f"journal={index.get('journal_mode') or 'unknown'}"
            ),
            (
                "Checks: quick_check="
                f"{(index.get('quick_check') or {}).get('status', 'unknown')}; "
                "foreign_key_check="
                f"{(index.get('foreign_key_check') or {}).get('status', 'unknown')}"
            ),
        ]
    )
