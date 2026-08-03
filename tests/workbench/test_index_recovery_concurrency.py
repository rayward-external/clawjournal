"""Concurrency regressions for the backup-first workbench index recovery."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from clawjournal import auto_upload as auto_upload_module
from clawjournal import config as config_module
from clawjournal.workbench import index as index_module
from clawjournal.workbench import index_recovery


@pytest.fixture
def recovery_state(tmp_path, monkeypatch) -> Path:
    """Keep the index, marker, blobs, and config inside one temporary root."""

    state = tmp_path / "state"
    monkeypatch.setattr(index_module, "CONFIG_DIR", state)
    monkeypatch.setattr(index_module, "INDEX_DB", state / "index.db")
    monkeypatch.setattr(index_module, "BLOBS_DIR", state / "blobs")
    monkeypatch.setattr(config_module, "CONFIG_DIR", state)
    monkeypatch.setattr(config_module, "CONFIG_FILE", state / "config.json")
    index_module._set_index_recovery_access(False)
    index_recovery._set_health(
        {"status": "ready", "message": "The test index is ready."}
    )
    yield state
    index_module._set_index_recovery_access(False)
    index_recovery._set_health(
        {"status": "ready", "message": "The test index is ready."}
    )


def _require_recovery(database: Path, **extra: object) -> None:
    health: dict[str, object] = {
        "status": "recovery_required",
        "message": "The test index needs recovery.",
        "database_path": str(database.resolve()),
        "automatic_recovery_available": True,
    }
    health.update(extra)
    index_recovery._set_health(health)


def _marker_path(state: Path) -> Path:
    return state / index_recovery.RECOVERY_MARKER_FILENAME


def test_begin_guided_rebuild_publishes_durable_marker_before_return(
    recovery_state,
):
    database = recovery_state / "index.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"damaged index bytes")
    _require_recovery(database)

    report = index_recovery.begin_guided_rebuild()

    marker_path = _marker_path(recovery_state)
    assert report["status"] == "rebuilding"
    assert marker_path.is_file()
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["database_path"] == str(database.resolve())
    assert isinstance(marker.get("stage"), str)


def test_connection_lease_file_preallocates_every_windows_lock_slot(
    recovery_state,
):
    lease = index_module._open_index_lease_file()
    lease.close()

    assert index_module._index_connection_lease_path().stat().st_size >= (
        index_module._INDEX_CONNECTION_LEASE_SLOTS
    )


def test_startup_health_fails_closed_when_connection_lease_is_unavailable(
    recovery_state,
    monkeypatch,
):
    conn = index_module.open_index()
    conn.close()

    def lease_denied(*_args, **_kwargs):
        raise PermissionError("connection lease denied")

    monkeypatch.setattr(index_module, "_open_index_lease_file", lease_denied)

    report = index_recovery.initialize_index_health()

    assert report["status"] == "unavailable"
    assert report["automatic_recovery_available"] is False
    assert "connection lease denied" in report["detail"]


def test_begin_guided_rebuild_preserves_verified_retry_backup(recovery_state):
    database = recovery_state / "index.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"partial rebuilt index")
    backup = recovery_state / "index-backups" / "known-good"
    backup.mkdir(parents=True)
    (backup / "index.db").write_bytes(b"original index bytes")
    index_recovery._write_marker(
        database,
        {
            "version": 1,
            "database_path": str(database.resolve()),
            "backup_path": str(backup),
            "stage": "failed",
            "error": "previous rescan failed",
        },
    )
    _require_recovery(
        database,
        interrupted_recovery=True,
        backup_path=str(backup),
    )

    index_recovery.begin_guided_rebuild()

    marker = json.loads(_marker_path(recovery_state).read_text(encoding="utf-8"))
    assert marker["backup_path"] == str(backup)
    assert (Path(marker["backup_path"]) / "index.db").read_bytes() == (
        b"original index bytes"
    )


def test_guided_rebuild_fails_closed_for_active_writer_then_retries(
    recovery_state,
    monkeypatch,
):
    database = recovery_state / "index.db"
    seed = index_module.open_index()
    try:
        seed.execute(
            "CREATE TABLE recovery_concurrency_probe "
            "(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        seed.execute(
            "INSERT INTO recovery_concurrency_probe VALUES (1, 'committed')"
        )
        seed.commit()
    finally:
        seed.close()

    real_exclusive = index_recovery._exclusive_source_connection
    real_backup = index_recovery._backup_index_files
    real_remove = index_recovery._remove_index_files
    backup_calls: list[Path] = []
    remove_calls: list[Path] = []

    def fast_exclusive(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(
            path.resolve().as_uri() + "?mode=rw",
            uri=True,
            timeout=0.05,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=50")
        try:
            conn.execute("BEGIN EXCLUSIVE")
        except Exception:
            conn.close()
            raise
        return conn

    def observed_backup(path: Path):
        backup_calls.append(path)
        return real_backup(path)

    def observed_remove(path: Path):
        remove_calls.append(path)
        return real_remove(path)

    monkeypatch.setattr(
        index_recovery,
        "_exclusive_source_connection",
        fast_exclusive,
    )
    monkeypatch.setattr(index_recovery, "_backup_index_files", observed_backup)
    monkeypatch.setattr(index_recovery, "_remove_index_files", observed_remove)

    writer = sqlite3.connect(database, timeout=0.1, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE recovery_concurrency_probe SET value = 'uncommitted' "
            "WHERE id = 1"
        )

        with pytest.raises(index_recovery.UnsafeIndexRecovery, match="still in use"):
            index_recovery.guided_rebuild(lambda: {"ok": True})

        assert backup_calls == []
        assert remove_calls == []
        assert database.is_file()
        assert _marker_path(recovery_state).is_file()
    finally:
        writer.rollback()
        writer.close()

    unchanged = sqlite3.connect(database)
    try:
        assert unchanged.execute(
            "SELECT value FROM recovery_concurrency_probe WHERE id = 1"
        ).fetchone()[0] == "committed"
    finally:
        unchanged.close()

    monkeypatch.setattr(
        index_recovery,
        "_exclusive_source_connection",
        real_exclusive,
    )
    result = index_recovery.guided_rebuild(lambda: {"ok": True})

    assert result["status"] == "ready"
    assert Path(result["backup_path"], "index.db").is_file()
    assert not _marker_path(recovery_state).exists()


def test_guided_rebuild_waits_for_guarded_connection_before_backup(
    recovery_state,
    monkeypatch,
):
    live = index_module.open_index()
    backup_started = threading.Event()
    real_backup = index_recovery._backup_index_files
    result: dict[str, object] = {}

    def observed_backup(path: Path):
        backup_started.set()
        return real_backup(path)

    def rebuild() -> None:
        try:
            result["value"] = index_recovery.guided_rebuild(lambda: {"ok": True})
        except BaseException as exc:  # surface worker errors in the main thread
            result["error"] = exc

    monkeypatch.setattr(index_recovery, "_backup_index_files", observed_backup)
    worker = threading.Thread(target=rebuild)
    worker.start()
    try:
        deadline = time.monotonic() + 2
        while not _marker_path(recovery_state).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _marker_path(recovery_state).exists()
        assert not backup_started.wait(0.1)
    finally:
        live.close()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert "error" not in result
    assert result["value"]["status"] == "ready"
    assert backup_started.is_set()


def test_save_config_does_not_touch_index_while_recovery_marker_exists(
    recovery_state,
    monkeypatch,
):
    database = recovery_state / "index.db"
    conn = index_module.open_index()
    conn.close()
    config_module.CONFIG_FILE.write_text(
        json.dumps({"excluded_projects": []}),
        encoding="utf-8",
    )
    index_recovery._write_marker(
        database,
        {
            "version": 1,
            "database_path": str(database.resolve()),
            "stage": "draining",
        },
    )
    profile_stamp_calls: list[sqlite3.Connection] = []
    monkeypatch.setattr(
        config_module,
        "mark_auto_upload_profile_changed",
        lambda connection: profile_stamp_calls.append(connection) or True,
    )

    saved = config_module.save_config({"excluded_projects": ["claude:private"]})

    assert saved is True
    assert json.loads(config_module.CONFIG_FILE.read_text(encoding="utf-8"))[
        "excluded_projects"
    ] == ["claude:private"]
    assert profile_stamp_calls == []


def test_save_config_keeps_success_after_connection_lease_oserror(
    recovery_state,
    monkeypatch,
    capsys,
):
    conn = index_module.open_index()
    conn.close()
    config_module.CONFIG_FILE.write_text(
        json.dumps({"excluded_projects": []}),
        encoding="utf-8",
    )

    def lease_denied(**_kwargs):
        raise PermissionError("connection lease denied")

    monkeypatch.setattr(index_module, "open_existing_index", lease_denied)

    saved = config_module.save_config({"excluded_projects": ["claude:private"]})

    assert saved is True
    assert json.loads(config_module.CONFIG_FILE.read_text(encoding="utf-8"))[
        "excluded_projects"
    ] == ["claude:private"]
    assert "could not pause automatic upload" in capsys.readouterr().err


def test_auto_upload_preview_fails_soft_when_connection_lease_is_unavailable(
    recovery_state,
    monkeypatch,
):
    conn = index_module.open_index()
    conn.close()

    def lease_denied(**_kwargs):
        raise PermissionError("connection lease denied")

    monkeypatch.setattr(index_module, "open_existing_index", lease_denied)

    result = auto_upload_module.preview(refresh=False)

    assert result["ok"] is False
    assert result["code"] == "index_upgrade_required"


def test_hook_direct_open_refuses_index_guarded_by_recovery_marker(
    recovery_state,
):
    database = recovery_state / "index.db"
    conn = index_module.open_index()
    conn.close()
    index_recovery._write_marker(
        database,
        {
            "version": 1,
            "database_path": str(database.resolve()),
            "stage": "draining",
        },
    )

    hook_connection = auto_upload_module._open_existing_hook_index()
    if hook_connection is not None:
        hook_connection.close()

    assert hook_connection is None
