"""Tests for the startup integrity guard and guided index recovery."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from threading import Thread

import pytest

from clawjournal import filesystem as filesystem_module
from clawjournal.workbench import index as index_module
from clawjournal.workbench import index_recovery


@pytest.fixture
def recovery_install(tmp_path, monkeypatch) -> Path:
    """Redirect every index artifact used by recovery to one temp root."""

    install_dir = tmp_path / "state"
    monkeypatch.setattr(index_module, "CONFIG_DIR", install_dir)
    monkeypatch.setattr(index_module, "INDEX_DB", install_dir / "index.db")
    monkeypatch.setattr(index_module, "BLOBS_DIR", install_dir / "blobs")
    index_recovery._set_health(
        {"status": "ready", "message": "Test index is ready."}
    )
    yield install_dir
    index_recovery._set_health(
        {"status": "ready", "message": "Test index is ready."}
    )


def _session(content: str = "Fix the recovery bug") -> dict:
    return {
        "session_id": "session-recovery",
        "project": "recovery-project",
        "source": "claude",
        "model": "claude-sonnet-4",
        "start_time": "2026-07-30T00:00:00+00:00",
        "end_time": "2026-07-30T00:10:00+00:00",
        "messages": [
            {"role": "user", "content": content, "tool_uses": []},
            {"role": "assistant", "content": "Done.", "tool_uses": []},
        ],
        "stats": {
            "user_messages": 1,
            "assistant_messages": 1,
            "tool_uses": 0,
            "input_tokens": 100,
            "output_tokens": 25,
        },
    }


def _scan_one_session() -> dict[str, object]:
    conn = index_module.open_index()
    try:
        index_module.upsert_sessions(conn, [_session()])
    finally:
        conn.close()
    return {"ok": True}


def _add_recovery_finding(conn: sqlite3.Connection, *, decided: bool) -> None:
    revision = conn.execute(
        "SELECT content_revision FROM sessions WHERE session_id = ?",
        ("session-recovery",),
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO findings (
            finding_id, session_id, engine, rule, entity_hash, field,
            offset, length, status, decided_by, decided_at,
            decision_reason, revision, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "finding-recovery",
            "session-recovery",
            "test-engine",
            "test-rule",
            "hash:test-entity",
            "content",
            0,
            4,
            "ignored" if decided else "open",
            "user" if decided else None,
            "2026-07-30T00:20:00+00:00" if decided else None,
            "Known test value" if decided else None,
            revision,
            "2026-07-30T00:15:00+00:00",
        ),
    )
    conn.commit()


def _scan_one_session_with_finding() -> dict[str, object]:
    conn = index_module.open_index()
    try:
        index_module.upsert_sessions(conn, [_session()])
        _add_recovery_finding(conn, decided=False)
    finally:
        conn.close()
    return {"ok": True}


def test_linux_mountinfo_uses_most_specific_mount_without_source_details():
    mountinfo = "\n".join((
        "36 25 0:32 / / rw,relatime - ext4 /dev/root rw",
        r"40 36 0:44 / /cluster\040home rw,relatime - nfs4 "
        r"server.example:/private/export rw",
    ))

    storage = filesystem_module._classify_linux_mountinfo(
        Path("/cluster home/user/.clawjournal/index.db"),
        mountinfo,
    )

    assert storage.health_fields() == {
        "filesystem_type": "nfs4",
        "storage_risk": "network",
        "storage_migration_required": True,
    }
    assert "server.example" not in repr(storage)
    assert "cluster home" not in repr(storage)


def test_filesystem_classifier_does_not_resolve_a_direct_network_path(
    monkeypatch,
):
    mountinfo = "\n".join((
        "36 25 0:32 / / rw,relatime - ext4 /dev/root rw",
        "40 36 0:44 / /cluster rw,relatime - nfs4 server:/private rw",
    ))
    monkeypatch.setattr(filesystem_module.sys, "platform", "linux")
    monkeypatch.setattr(
        filesystem_module.os.path,
        "abspath",
        lambda _path: "/cluster/users/alice/.clawjournal/index.db",
    )
    monkeypatch.setattr(
        filesystem_module,
        "_read_linux_mountinfo",
        lambda: mountinfo,
    )
    monkeypatch.setattr(
        filesystem_module.Path,
        "resolve",
        lambda *_args, **_kwargs: pytest.fail(
            "a directly mounted network path must not be resolved"
        ),
    )

    storage = filesystem_module.classify_filesystem(
        Path("/cluster/users/alice/.clawjournal/index.db")
    )

    assert storage == filesystem_module.FilesystemInfo("nfs4", "network")


def test_filesystem_classifier_refreshes_mounts_after_resolving_symlink(
    monkeypatch,
):
    initial = "36 25 0:32 / / rw,relatime - ext4 /dev/root rw"
    refreshed = "\n".join((
        initial,
        "40 36 0:44 / /autofs/home rw,relatime - nfs server:/home rw",
    ))
    snapshots = iter((initial, refreshed))
    monkeypatch.setattr(filesystem_module.sys, "platform", "linux")
    monkeypatch.setattr(
        filesystem_module,
        "_read_linux_mountinfo",
        lambda: next(snapshots),
    )
    monkeypatch.setattr(
        filesystem_module.Path,
        "resolve",
        lambda *_args, **_kwargs: Path("/autofs/home/alice/index.db"),
    )

    storage = filesystem_module.classify_filesystem(Path("/home/alice/index.db"))

    assert storage == filesystem_module.FilesystemInfo("nfs", "network")


def test_filesystem_classifier_degrades_on_legacy_symlink_loop_error(
    monkeypatch,
):
    mountinfo = "36 25 0:32 / / rw,relatime - ext4 /dev/root rw"
    monkeypatch.setattr(filesystem_module.sys, "platform", "linux")
    monkeypatch.setattr(
        filesystem_module.os.path,
        "abspath",
        lambda _path: "/local/index.db",
    )
    monkeypatch.setattr(
        filesystem_module,
        "_read_linux_mountinfo",
        lambda: mountinfo,
    )
    monkeypatch.setattr(
        filesystem_module.Path,
        "resolve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("symlink loop")
        ),
    )

    storage = filesystem_module.classify_filesystem(Path("/local/index.db"))

    assert storage == filesystem_module.FilesystemInfo("ext4", "local")


def test_linux_mountinfo_prefers_visible_overmount_at_same_path():
    mountinfo = "\n".join((
        "36 25 0:32 / / rw,relatime - ext4 /dev/root rw",
        "40 36 0:44 / /cluster rw,relatime - ext4 /dev/local rw",
        "57 36 0:45 / /cluster rw,relatime - nfs4 server:/cluster rw",
    ))

    storage = filesystem_module._classify_linux_mountinfo(
        Path("/cluster/users/alice/index.db"),
        mountinfo,
    )

    assert storage == filesystem_module.FilesystemInfo("nfs4", "network")


def test_linux_mountinfo_never_uses_mount_id_to_hide_network_candidate():
    mountinfo = "\n".join((
        "36 25 0:32 / / rw,relatime - ext4 /dev/root rw",
        "100 36 0:44 / /cluster rw,relatime - ext4 /dev/local rw",
        "40 36 0:45 / /cluster rw,relatime - nfs server:/cluster rw",
    ))

    storage = filesystem_module._classify_linux_mountinfo(
        Path("/cluster/users/alice/index.db"),
        mountinfo,
    )

    assert storage == filesystem_module.FilesystemInfo("nfs", "network")


@pytest.mark.parametrize(
    "filesystem_type",
    ("../../private", "evil<script>", "x" * 33, "fuse.alice_private"),
)
def test_malformed_filesystem_type_is_redacted_to_unknown(filesystem_type):
    storage = filesystem_module._filesystem_info(filesystem_type)

    assert storage.health_fields() == {
        "filesystem_type": "unknown",
        "storage_risk": "unknown",
        "storage_migration_required": False,
    }
    assert filesystem_type not in repr(storage)


@pytest.mark.parametrize(
    ("filesystem_type", "risk"),
    (("ext4", "local"), ("overlay", "unknown")),
)
def test_unknown_and_local_filesystem_kinds_are_not_blocked(
    recovery_install,
    monkeypatch,
    filesystem_type,
    risk,
):
    monkeypatch.setattr(
        filesystem_module,
        "classify_filesystem",
        lambda _path: filesystem_module.FilesystemInfo(filesystem_type, risk),
    )

    conn = index_module.open_index()
    conn.close()

    assert (recovery_install / "index.db").is_file()


def test_missing_index_on_network_storage_fails_closed_without_creating_files(
    recovery_install,
    monkeypatch,
):
    monkeypatch.setattr(
        filesystem_module,
        "classify_filesystem",
        lambda _path: filesystem_module.FilesystemInfo("nfs4", "network"),
    )

    report = index_recovery.inspect_index_health()

    assert report["status"] == "unavailable"
    assert report["code"] == "storage_migration_required"
    assert report["filesystem_type"] == "nfs4"
    assert report["storage_risk"] == "network"
    assert report["storage_migration_required"] is True
    assert report["automatic_recovery_available"] is False
    assert "CLAWJOURNAL_HOME" in report["message"]
    assert not (recovery_install / "index.db").exists()
    assert not (recovery_install / "index-connections.lock").exists()


def test_startup_health_refuses_network_storage_without_resolving_it(
    recovery_install,
    monkeypatch,
):
    monkeypatch.setattr(
        filesystem_module,
        "classify_filesystem",
        lambda _path: filesystem_module.FilesystemInfo("nfs4", "network"),
    )
    monkeypatch.setattr(
        index_recovery.Path,
        "resolve",
        lambda *_args, **_kwargs: pytest.fail(
            "startup must not resolve a known hard-NFS path"
        ),
    )

    report = index_recovery.begin_index_health_check()

    assert report["status"] == "unavailable"
    assert report["code"] == "storage_migration_required"
    assert report["storage_migration_required"] is True


def test_healthy_existing_index_on_network_storage_is_not_inspected(
    recovery_install,
    monkeypatch,
):
    conn = index_module.open_index()
    conn.close()
    database = recovery_install / "index.db"
    original = database.read_bytes()

    monkeypatch.setattr(
        filesystem_module,
        "classify_filesystem",
        lambda _path: filesystem_module.FilesystemInfo("cifs", "network"),
    )
    monkeypatch.setattr(
        index_recovery,
        "_health_connection",
        lambda _path: pytest.fail("network storage must not be opened"),
    )

    report = index_recovery.inspect_index_health()

    assert report["status"] == "unavailable"
    assert report["filesystem_type"] == "cifs"
    assert report["storage_migration_required"] is True
    assert database.read_bytes() == original


def test_open_index_rejects_network_storage_before_bootstrap(
    recovery_install,
    monkeypatch,
):
    monkeypatch.setattr(
        filesystem_module,
        "classify_filesystem",
        lambda _path: filesystem_module.FilesystemInfo("lustre", "network"),
    )

    with pytest.raises(index_module.UnsafeIndexStorageError) as exc_info:
        index_module.open_index()

    assert "CLAWJOURNAL_HOME" in str(exc_info.value)
    assert "lustre" in str(exc_info.value)
    assert not (recovery_install / "index.db").exists()
    assert not (recovery_install / "index-connections.lock").exists()
    assert not (recovery_install / "blobs").exists()


def test_all_leased_index_opens_are_blocked_but_lease_free_evidence_is_readable(
    recovery_install,
    monkeypatch,
):
    database = recovery_install / "index.db"
    recovery_install.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE sample (value TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        filesystem_module,
        "classify_filesystem",
        lambda _path: filesystem_module.FilesystemInfo("ceph", "network"),
    )

    with pytest.raises(index_module.UnsafeIndexStorageError):
        index_module.open_existing_index(database=database, readonly=True)
    with pytest.raises(index_module.UnsafeIndexStorageError):
        index_module.open_existing_index(database=database)

    evidence = index_recovery._read_only_connection(database)
    evidence.close()
    assert not (recovery_install / "index-connections.lock").exists()


def test_begin_guided_rebuild_rechecks_storage_before_creating_marker(
    recovery_install,
    monkeypatch,
):
    index_recovery._set_health({
        "status": "recovery_required",
        "message": "Test damage",
        "automatic_recovery_available": True,
    })
    monkeypatch.setattr(
        filesystem_module,
        "classify_filesystem",
        lambda _path: filesystem_module.FilesystemInfo("sshfs", "network"),
    )

    with pytest.raises(index_recovery.UnsafeIndexRecovery) as exc_info:
        index_recovery.begin_guided_rebuild()

    assert "CLAWJOURNAL_HOME" in str(exc_info.value)
    health = index_recovery.current_index_health()
    assert health["status"] == "unavailable"
    assert health["code"] == "storage_migration_required"
    assert health["storage_migration_required"] is True
    assert not (recovery_install / index_recovery.RECOVERY_MARKER_FILENAME).exists()


def test_direct_guided_rebuild_rechecks_storage_before_backup(
    recovery_install,
    monkeypatch,
):
    callback_called = False

    def scan_callback():
        nonlocal callback_called
        callback_called = True
        return {"ok": True}

    monkeypatch.setattr(
        filesystem_module,
        "classify_filesystem",
        lambda _path: filesystem_module.FilesystemInfo("smbfs", "network"),
    )

    with pytest.raises(index_recovery.UnsafeIndexRecovery):
        index_recovery.guided_rebuild(scan_callback)

    assert callback_called is False
    assert not (recovery_install / index_recovery.RECOVERY_MARKER_FILENAME).exists()
    assert not (recovery_install / "index-backups").exists()


def test_guided_rebuild_rechecks_storage_next_to_source_rw_open(
    recovery_install,
    monkeypatch,
):
    database = recovery_install / "index.db"
    recovery_install.mkdir(parents=True, exist_ok=True)
    original = b"damaged sqlite bytes"
    database.write_bytes(original)
    classification_calls = 0

    def changing_storage(_path):
        nonlocal classification_calls
        classification_calls += 1
        if classification_calls <= 2:
            return filesystem_module.FilesystemInfo("ext4", "local")
        return filesystem_module.FilesystemInfo("nfs", "network")

    monkeypatch.setattr(
        filesystem_module,
        "classify_filesystem",
        changing_storage,
    )
    monkeypatch.setattr(
        index_recovery.sqlite3,
        "connect",
        lambda *_args, **_kwargs: pytest.fail(
            "network storage must be rejected before SQLite opens it read/write"
        ),
    )

    with pytest.raises(index_recovery.UnsafeIndexRecovery):
        index_recovery.guided_rebuild(lambda: {"ok": True})

    assert classification_calls >= 3
    assert database.read_bytes() == original
    assert not (recovery_install / "index-backups").exists()
    assert index_recovery.current_index_health()["code"] == (
        "storage_migration_required"
    )
    assert index_recovery._load_marker(database)["stage"] == "preparing"


def test_remount_after_fresh_backup_preserves_live_index(
    recovery_install,
    monkeypatch,
):
    conn = index_module.open_index()
    conn.close()
    database = recovery_install / "index.db"
    original = database.read_bytes()
    index_recovery._set_health({
        "status": "recovery_required",
        "automatic_recovery_available": True,
        "message": "test recovery",
    })
    remounted = False
    real_backup = index_recovery._backup_index_files

    def backup_then_remount(path):
        nonlocal remounted
        result = real_backup(path)
        remounted = True
        return result

    monkeypatch.setattr(index_recovery, "_backup_index_files", backup_then_remount)
    monkeypatch.setattr(
        filesystem_module,
        "classify_filesystem",
        lambda _path: filesystem_module.FilesystemInfo(
            "nfs4" if remounted else "ext4",
            "network" if remounted else "local",
        ),
    )

    with pytest.raises(index_recovery.UnsafeIndexRecovery):
        index_recovery.guided_rebuild(
            lambda: pytest.fail("a remounted recovery must not start scanning")
        )

    assert database.is_file()
    assert database.read_bytes() == original
    assert index_recovery._load_marker(database)["stage"] == "preparing"
    assert index_recovery.current_index_health()["code"] == (
        "storage_migration_required"
    )


def test_remount_after_existing_backup_snapshot_preserves_live_index(
    recovery_install,
    monkeypatch,
):
    conn = index_module.open_index()
    conn.close()
    database = recovery_install / "index.db"
    original = database.read_bytes()
    backup_name = "index-recovery-20260816T020304Z-1234abcd"
    backup = recovery_install / "index-backups" / backup_name
    backup.mkdir(parents=True)
    (backup / "index.db").write_bytes(original)
    (backup / "recovery-manifest.json").write_text("{}", encoding="utf-8")
    index_recovery._write_marker(
        database,
        {
            "version": 2,
            "database_path": str(database.resolve()),
            "backup_path": str(backup.resolve()),
            "backup_directory": backup_name,
            "stage": "failed",
        },
    )
    index_recovery._set_health({
        "status": "recovery_required",
        "automatic_recovery_available": True,
        "message": "retry recovery",
    })
    remounted = False
    real_snapshot = index_recovery._snapshot_from_path

    def snapshot_then_remount(path):
        nonlocal remounted
        result = real_snapshot(path)
        remounted = True
        return result

    monkeypatch.setattr(index_recovery, "_snapshot_from_path", snapshot_then_remount)
    monkeypatch.setattr(
        filesystem_module,
        "classify_filesystem",
        lambda _path: filesystem_module.FilesystemInfo(
            "nfs" if remounted else "ext4",
            "network" if remounted else "local",
        ),
    )

    with pytest.raises(index_recovery.UnsafeIndexRecovery):
        index_recovery.guided_rebuild(
            lambda: pytest.fail("a remounted retry must not start scanning")
        )

    assert database.is_file()
    assert database.read_bytes() == original
    assert index_recovery._load_marker(database)["stage"] == "failed"
    assert index_recovery.current_index_health()["code"] == (
        "storage_migration_required"
    )


def test_remove_index_files_rechecks_storage_before_unlink(
    recovery_install,
    monkeypatch,
):
    database = recovery_install / "index.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"live index")
    journal = Path(str(database) + "-journal")
    journal.write_bytes(b"live journal")
    monkeypatch.setattr(
        filesystem_module,
        "classify_filesystem",
        lambda _path: filesystem_module.FilesystemInfo("nfs4", "network"),
    )

    with pytest.raises(index_recovery.UnsafeIndexRecovery):
        index_recovery._remove_index_files(database)

    assert database.read_bytes() == b"live index"
    assert journal.read_bytes() == b"live journal"


def test_initialize_new_index_uses_delete_journal(recovery_install):
    report = index_recovery.initialize_index_health()

    assert report["status"] == "ready"
    assert report["journal_mode"] == "delete"
    assert (recovery_install / "index.db").is_file()

    conn = sqlite3.connect(recovery_install / "index.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()


def test_begin_index_health_check_is_fail_closed_and_not_reinspected(
    recovery_install,
    monkeypatch,
):
    """Requests must see a stable non-ready state while startup inspection runs."""

    health = index_recovery.begin_index_health_check()

    assert health["status"] == "checking"
    assert health["automatic_recovery_available"] is False
    assert health["database_path"] == str(
        (recovery_install / "index.db").resolve()
    )
    assert index_recovery.current_index_health() == health

    # A stale cross-process recovery marker must not make an HTTP request run a
    # second integrity check concurrently with the startup check.
    monkeypatch.setattr(index_recovery, "recovery_marker_exists", lambda: True)
    monkeypatch.setattr(
        index_recovery,
        "initialize_index_health",
        lambda: pytest.fail("checking health must not be reinspected"),
    )

    assert index_recovery.synchronize_index_health() == health


def test_initialize_unexpected_inspection_error_becomes_unavailable(
    recovery_install,
    monkeypatch,
):
    """An ordinary startup exception must fail closed instead of leaking checking."""

    index_recovery.begin_index_health_check()
    monkeypatch.setattr(
        index_recovery,
        "inspect_index_health",
        lambda: (_ for _ in ()).throw(RuntimeError("unexpected health failure")),
    )
    monkeypatch.setattr(
        index_module,
        "open_index",
        lambda: pytest.fail("an unchecked index must never be opened"),
    )

    report = index_recovery.initialize_index_health()

    assert report["status"] == "unavailable"
    assert report["automatic_recovery_available"] is False
    assert report["detail"] == "unexpected health failure"
    assert report["database_path"] == str(
        (recovery_install / "index.db").resolve()
    )
    assert index_recovery.current_index_health() == report


def test_initialize_unexpected_open_error_becomes_unavailable(
    recovery_install,
    monkeypatch,
):
    """The post-inspection schema/bootstrap open is part of the same gate."""

    index_recovery.begin_index_health_check()
    monkeypatch.setattr(
        index_recovery,
        "inspect_index_health",
        lambda: {
            "status": "ready",
            "message": "The test index passed inspection.",
            "database_path": str((recovery_install / "index.db").resolve()),
            "automatic_recovery_available": False,
        },
    )
    monkeypatch.setattr(
        index_module,
        "open_index",
        lambda: (_ for _ in ()).throw(RuntimeError("unexpected open failure")),
    )

    report = index_recovery.initialize_index_health()

    assert report["status"] == "unavailable"
    assert report["automatic_recovery_available"] is False
    assert report["detail"] == "unexpected open failure"
    assert index_recovery.current_index_health() == report


def test_initialize_healthy_wal_index_converts_to_delete(recovery_install):
    database = recovery_install / "index.db"
    database.parent.mkdir(parents=True)
    legacy = index_module.open_index()
    try:
        assert legacy.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        legacy.execute("CREATE TABLE legacy_row (value TEXT)")
        legacy.execute("INSERT INTO legacy_row VALUES ('preserved')")
        legacy.commit()
    finally:
        legacy.close()

    report = index_recovery.initialize_index_health()

    assert report["status"] == "ready"
    assert report["journal_mode"] == "delete"
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("SELECT value FROM legacy_row").fetchone()[0] == "preserved"
    finally:
        conn.close()


def test_non_sqlite_index_is_recoverable(recovery_install):
    database = recovery_install / "index.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not a sqlite database")

    report = index_recovery.inspect_index_health()

    assert report["status"] == "recovery_required"
    assert report["automatic_recovery_available"] is True
    assert report["unreadable_state"]


def test_locked_index_is_not_misclassified_as_corruption(
    recovery_install,
    monkeypatch,
):
    database = recovery_install / "index.db"
    database.parent.mkdir(parents=True)
    seed = sqlite3.connect(database)
    try:
        seed.execute("CREATE TABLE placeholder (value TEXT)")
        seed.commit()
    finally:
        seed.close()

    def locked_connection(_path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        index_recovery,
        "_health_connection",
        locked_connection,
    )

    report = index_recovery.inspect_index_health()

    assert report["status"] == "unavailable"
    assert report["automatic_recovery_available"] is False


def test_health_check_rolls_back_hot_delete_journal(recovery_install):
    database = recovery_install / "index.db"
    conn = index_module.open_index()
    try:
        conn.execute("CREATE TABLE hot_journal_probe (id TEXT, value TEXT)")
        conn.executemany(
            "INSERT INTO hot_journal_probe VALUES (?, ?)",
            [(str(index), "a" * 4000) for index in range(50)],
        )
        conn.commit()
    finally:
        conn.close()

    child = """
import os
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA journal_mode=DELETE")
conn.execute("PRAGMA synchronous=FULL")
conn.execute("PRAGMA cache_size=1")
conn.execute("BEGIN IMMEDIATE")
conn.execute("UPDATE hot_journal_probe SET value = replace(value, 'a', 'b')")
os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-c", child, str(database)],
        check=True,
        env={**os.environ},
    )
    journal = Path(str(database) + "-journal")
    assert journal.is_file()
    assert journal.stat().st_size > 0

    report = index_recovery.inspect_index_health()

    assert report["status"] == "ready"
    assert not journal.exists()
    verified = sqlite3.connect(database)
    try:
        assert verified.execute(
            "SELECT DISTINCT substr(value, 1, 1) FROM hot_journal_probe"
        ).fetchall() == [("a",)]
    finally:
        verified.close()


def test_malformed_recovery_marker_fails_closed(recovery_install):
    marker = recovery_install / index_recovery.RECOVERY_MARKER_FILENAME
    marker.parent.mkdir(parents=True)
    marker.write_text("not-json", encoding="utf-8")

    report = index_recovery.inspect_index_health()

    assert report["status"] == "unavailable"
    assert report["automatic_recovery_available"] is False
    assert report["interrupted_recovery"] is True


def test_marker_with_missing_backup_fails_closed(recovery_install):
    database = recovery_install / "index.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"damaged source")
    index_recovery._write_marker(
        database,
        {
            "version": 1,
            "stage": "backed_up",
            "backup_path": str(recovery_install / "missing-backup"),
        },
    )

    report = index_recovery.inspect_index_health()

    assert report["status"] == "unavailable"
    assert report["automatic_recovery_available"] is False
    assert report["interrupted_recovery"] is True


@pytest.mark.parametrize("marker_version", (1, 2))
def test_complete_state_copy_rebases_interrupted_recovery_backup(
    recovery_install,
    tmp_path,
    monkeypatch,
    marker_version,
):
    old_state = recovery_install
    database = old_state / "index.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"partial rebuilt index")
    backup_name = "index-recovery-20260816T010203Z-abcdef12"
    backup = old_state / "index-backups" / backup_name
    backup.mkdir(parents=True)
    (backup / "index.db").write_bytes(b"original index bytes")
    (backup / "recovery-manifest.json").write_text("{}", encoding="utf-8")
    marker = {
        "version": marker_version,
        "database_path": str(database.resolve()),
        "backup_path": str(backup.resolve()),
        "stage": "failed",
    }
    if marker_version == 2:
        marker["backup_directory"] = backup_name
    index_recovery._write_marker(database, marker)

    local_state = tmp_path / "local-state"
    shutil.copytree(old_state, local_state)
    old_state.rename(tmp_path / "disconnected-network-state")
    monkeypatch.setattr(index_module, "CONFIG_DIR", local_state)
    monkeypatch.setattr(index_module, "INDEX_DB", local_state / "index.db")
    monkeypatch.setattr(index_module, "BLOBS_DIR", local_state / "blobs")

    report = index_recovery.inspect_index_health()

    expected_backup = local_state / "index-backups" / backup_name
    assert report["status"] == "recovery_required"
    assert report["automatic_recovery_available"] is True
    assert Path(report["backup_path"]) == expected_backup.resolve()
    assert (expected_backup / "index.db").read_bytes() == b"original index bytes"

    result = index_recovery.guided_rebuild(_scan_one_session)

    assert result["status"] == "ready"
    assert Path(result["backup_path"]) == expected_backup.resolve()
    assert not (
        local_state / index_recovery.RECOVERY_MARKER_FILENAME
    ).exists()
    rebuilt = index_module.open_index()
    try:
        assert rebuilt.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id = ?",
            ("session-recovery",),
        ).fetchone()[0] == 1
    finally:
        rebuilt.close()


def test_v2_marker_rejects_non_generated_backup_identity(recovery_install):
    database = recovery_install / "index.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"partial rebuilt index")
    index_recovery._write_marker(
        database,
        {
            "version": 2,
            "backup_directory": "../outside",
            "stage": "failed",
        },
    )

    report = index_recovery.inspect_index_health()

    assert report["status"] == "unavailable"
    assert report["automatic_recovery_available"] is False
    assert report["interrupted_recovery"] is True


def test_unknown_recovery_marker_version_fails_closed(recovery_install):
    database = recovery_install / "index.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"partial rebuilt index")
    backup = recovery_install / "index-backups" / "legacy-backup"
    backup.mkdir(parents=True)
    (backup / "index.db").write_bytes(b"original index")
    index_recovery._write_marker(
        database,
        {
            "version": 999,
            "backup_path": str(backup),
            "stage": "failed",
        },
    )

    report = index_recovery.inspect_index_health()

    assert report["status"] == "unavailable"
    assert report["automatic_recovery_available"] is False
    assert "version is unsupported" in report["detail"]


def test_backup_root_symlink_cannot_escape_state_directory(
    recovery_install,
    tmp_path,
):
    database = recovery_install / "index.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"damaged index")
    outside = tmp_path / "outside-backups"
    outside.mkdir()
    try:
        (recovery_install / "index-backups").symlink_to(
            outside,
            target_is_directory=True,
        )
    except OSError:
        pytest.skip("directory symlinks are not available on this platform")

    with pytest.raises(
        index_recovery.UnsafeIndexRecovery,
        match="outside the ClawJournal state root",
    ):
        index_recovery._backup_index_files(database)

    assert list(outside.iterdir()) == []


def test_marker_with_only_sidecar_backup_fails_closed(recovery_install):
    database = recovery_install / "index.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"damaged source")
    backup = recovery_install / "index-backups" / "sidecar-only"
    backup.mkdir(parents=True)
    (backup / "index.db-wal").write_bytes(b"orphaned wal")
    index_recovery._write_marker(
        database,
        {"version": 1, "stage": "backed_up", "backup_path": str(backup)},
    )

    report = index_recovery.inspect_index_health()

    assert report["status"] == "unavailable"
    assert report["automatic_recovery_available"] is False
    assert "database backup is missing" in report["detail"]


def test_recovery_marker_blocks_direct_index_access(recovery_install):
    conn = index_module.open_index()
    conn.close()
    database = recovery_install / "index.db"
    index_recovery._write_marker(
        database,
        {"version": 1, "stage": "preparing"},
    )

    with pytest.raises(sqlite3.DatabaseError, match="unfinished recovery"):
        index_module.open_index()

    outcomes: list[str] = []
    previous = index_module._set_index_recovery_access(True)
    try:
        allowed = index_module.open_index()
        allowed.close()

        def open_from_other_thread() -> None:
            try:
                unexpected = index_module.open_index()
            except sqlite3.DatabaseError:
                outcomes.append("blocked")
            else:
                unexpected.close()
                outcomes.append("opened")

        thread = Thread(target=open_from_other_thread)
        thread.start()
        thread.join(timeout=2)
    finally:
        index_module._set_index_recovery_access(previous)

    assert outcomes == ["blocked"]
    with pytest.raises(sqlite3.DatabaseError, match="unfinished recovery"):
        index_module.open_index()


def test_interrupted_preparing_stage_retries_from_untouched_index(
    recovery_install,
):
    database = recovery_install / "index.db"
    database.parent.mkdir(parents=True)
    original = b"damaged but untouched before backup"
    database.write_bytes(original)
    index_recovery._write_marker(
        database,
        {"version": 1, "stage": "preparing"},
    )

    report = index_recovery.inspect_index_health()
    assert report["status"] == "recovery_required"
    assert report["backup_path"] is None
    assert "has not been replaced" in report["message"]

    result = index_recovery.guided_rebuild(_scan_one_session)

    assert (Path(result["backup_path"]) / "index.db").read_bytes() == original


def test_preparing_marker_without_original_index_is_unavailable(
    recovery_install,
):
    database = recovery_install / "index.db"
    database.parent.mkdir(parents=True)
    index_recovery._write_marker(
        database,
        {"version": 1, "stage": "preparing"},
    )

    report = index_recovery.inspect_index_health()

    assert report["status"] == "unavailable"
    assert report["automatic_recovery_available"] is False
    assert "original index is missing" in report["detail"]


def test_snapshot_treats_missing_critical_session_columns_as_unreadable():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "CREATE TABLE sessions ("
            "session_id TEXT PRIMARY KEY, review_status TEXT, "
            "content_revision TEXT)"
        )

        snapshot, errors = index_recovery._recovery_snapshot(conn)
    finally:
        conn.close()

    assert snapshot["sessions"] == []
    assert errors == ["session decisions and hold state"]


def test_recovery_preserves_missing_checkpoint_projection_metadata(
    recovery_install,
):
    conn = index_module.open_index()
    try:
        session = _session()
        session.update({
            "logical_session_id": "session-recovery",
            "parent_session_id": "real-parent",
            "segment_index": 0,
            "segment_message_range": [0, 1],
            "segment_reason": "bounded_checkpoint",
            "segment_sealed": True,
            "raw_source_start_offset": 0,
            "raw_source_end_offset": 42,
        })
        index_module.upsert_sessions(conn, [session])
        conn.execute(
            "UPDATE sessions SET checkpoint_active = 0 WHERE session_id = ?",
            ("session-recovery",),
        )
        conn.commit()
        snapshot, errors = index_recovery._recovery_snapshot(conn)
        assert errors == []
        saved = snapshot["sessions"][0]
        assert saved["logical_session_id"] == "session-recovery"
        assert saved["checkpoint_active"] == 0

        conn.execute(
            "DELETE FROM sessions WHERE session_id = ?", ("session-recovery",)
        )
        conn.commit()
        restored, needs_review, skipped = index_recovery._restore_session_state(
            conn, snapshot["sessions"]
        )
        conn.commit()

        assert (restored, needs_review, skipped) == (0, 1, 0)
        row = conn.execute(
            "SELECT parent_session_id, segment_index, segment_start_message, "
            "segment_end_message, segment_reason, segment_sealed, "
            "raw_source_start_offset, raw_source_end_offset, logical_session_id, "
            "checkpoint_active, logical_revision FROM sessions "
            "WHERE session_id = 'session-recovery'"
        ).fetchone()
        assert dict(row) == {
            "parent_session_id": "real-parent",
            "segment_index": 0,
            "segment_start_message": 0,
            "segment_end_message": 1,
            "segment_reason": "bounded_checkpoint",
            "segment_sealed": 1,
            "raw_source_start_offset": 0,
            "raw_source_end_offset": 42,
            "logical_session_id": "session-recovery",
            "checkpoint_active": 0,
            "logical_revision": saved["logical_revision"],
        }
    finally:
        conn.close()


def test_snapshot_treats_missing_critical_finding_columns_as_unreadable():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "CREATE TABLE sessions ("
            "session_id TEXT PRIMARY KEY, review_status TEXT, "
            "hold_state TEXT, content_revision TEXT)"
        )
        conn.execute(
            "CREATE TABLE findings ("
            "session_id TEXT, engine TEXT, entity_hash TEXT, status TEXT)"
        )

        snapshot, errors = index_recovery._recovery_snapshot(conn)
    finally:
        conn.close()

    assert snapshot["sessions"] == []
    assert snapshot["finding_decisions"] == []
    assert errors == ["finding decisions"]


def test_snapshot_without_sessions_keeps_other_readable_state():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "CREATE TABLE policies ("
            "policy_id TEXT PRIMARY KEY, policy_type TEXT NOT NULL, "
            "value TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO policies VALUES (?, ?, ?, ?, ?)",
            (
                "policy-recovery",
                "redact_string",
                "keep-this-policy",
                "Readable despite session damage",
                "2026-07-30T00:00:00+00:00",
            ),
        )

        snapshot, errors = index_recovery._recovery_snapshot(conn)
    finally:
        conn.close()

    assert snapshot["sessions"] == []
    assert snapshot["policies"][0]["value"] == "keep-this-policy"
    assert errors == ["session decisions and hold state"]


def test_existing_database_without_sessions_requires_recovery(recovery_install):
    database = recovery_install / "index.db"
    database.parent.mkdir(parents=True)
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "CREATE TABLE policies ("
            "policy_id TEXT PRIMARY KEY, policy_type TEXT NOT NULL, "
            "value TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO policies VALUES "
            "('policy-recovery', 'redact_string', 'preserve-me', NULL, 'now')"
        )
        conn.commit()
    finally:
        conn.close()

    report = index_recovery.inspect_index_health()

    assert report["status"] == "recovery_required"
    assert report["recoverable_state_counts"]["policies"] == 1
    assert "sessions table" in report["detail"]


def test_zero_byte_index_is_backed_up_before_rebuild(recovery_install):
    database = recovery_install / "index.db"
    database.parent.mkdir(parents=True)
    database.touch()

    report = index_recovery.inspect_index_health()
    assert report["status"] == "recovery_required"
    assert report["automatic_recovery_available"] is True

    result = index_recovery.guided_rebuild(_scan_one_session)

    backup = Path(result["backup_path"]) / "index.db"
    assert backup.is_file()
    assert backup.stat().st_size == 0
    rebuilt = index_module.open_index()
    try:
        row = rebuilt.execute(
            "SELECT review_status, hold_state FROM sessions"
        ).fetchone()
        assert tuple(row) == ("new", "pending_review")
    finally:
        rebuilt.close()


def test_marker_transition_refreshes_cached_health(recovery_install):
    conn = index_module.open_index()
    conn.close()
    database = recovery_install / "index.db"
    backup = recovery_install / "index-backups" / "external-recovery"
    backup.mkdir(parents=True)
    (backup / "index.db").write_bytes(database.read_bytes())
    index_recovery._write_marker(
        database,
        {
            "version": 1,
            "backup_path": str(backup),
            "stage": "backed_up",
        },
    )
    index_recovery._set_health({"status": "ready", "message": "cached"})

    blocked = index_recovery.synchronize_index_health()
    assert blocked["status"] == "recovery_required"
    assert blocked["interrupted_recovery"] is True

    (recovery_install / index_recovery.RECOVERY_MARKER_FILENAME).unlink()
    resumed = index_recovery.synchronize_index_health()
    assert resumed["status"] == "ready"


def test_runtime_network_remount_replaces_cached_ready_health(
    recovery_install,
    monkeypatch,
):
    index_recovery._set_health({
        "status": "ready",
        "message": "cached local health",
        "storage_risk": "local",
        "storage_migration_required": False,
    })
    monkeypatch.setattr(
        filesystem_module,
        "classify_filesystem",
        lambda _path: filesystem_module.FilesystemInfo("nfs4", "network"),
    )
    monkeypatch.setattr(
        index_recovery,
        "recovery_marker_exists",
        lambda *_args, **_kwargs: pytest.fail(
            "a network remount must fail closed before marker inspection"
        ),
    )

    blocked = index_recovery.synchronize_index_health()

    assert blocked["status"] == "unavailable"
    assert blocked["code"] == "storage_migration_required"
    assert blocked["storage_risk"] == "network"
    assert blocked["storage_migration_required"] is True
    assert index_recovery.current_index_health() == blocked


def test_unknown_storage_does_not_clear_cached_network_block(
    recovery_install,
    monkeypatch,
):
    blocked = index_recovery._set_health({
        "status": "unavailable",
        "code": "storage_migration_required",
        "storage_risk": "network",
        "storage_migration_required": True,
    })
    monkeypatch.setattr(
        filesystem_module,
        "classify_filesystem",
        lambda _path: filesystem_module.FilesystemInfo("unknown", "unknown"),
    )
    monkeypatch.setattr(
        index_recovery,
        "initialize_index_health",
        lambda: pytest.fail("unknown storage must not clear a network block"),
    )

    assert index_recovery.synchronize_index_health() == blocked


def test_legacy_enrollment_scope_is_restored_paused():
    warnings: list[str] = []
    rows = index_recovery._safe_enrollment_rows(
        [{
            "singleton_id": 1,
            "mode": "enabled",
            "health": "ready",
            "generation": 2,
            "enrolled_at": "2026-07-30T00:00:00+00:00",
            "client_enrollment_id": "legacy-client",
            "enrolled_sources_json": '["claude"]',
            "enrolled_projects_json": '["project-a"]',
        }],
        warnings,
    )

    assert rows[0]["mode"] == "paused"
    assert rows[0]["health"] == "action_required"
    assert rows[0]["enrolled_scope_entries_json"] == '[["claude","project-a"]]'
    assert warnings == [
        "Automatic uploads were restored paused and must be reviewed."
    ]


def test_invalid_exact_enrollment_scope_is_left_disabled_in_backup():
    warnings: list[str] = []
    rows = index_recovery._safe_enrollment_rows(
        [{
            "mode": "enabled",
            "enrolled_at": "2026-07-30T00:00:00+00:00",
            "client_enrollment_id": "invalid-client",
            "enrolled_sources_json": '["claude"]',
            "enrolled_projects_json": '["project-a"]',
            "enrolled_scope_entries_json": '[["codex","project-b"]]',
        }],
        warnings,
    )

    assert rows == []
    assert warnings == [
        "Unreadable automatic-upload state was left disabled in the backup."
    ]


def test_backup_copies_database_and_all_sqlite_sidecars(recovery_install):
    database = recovery_install / "index.db"
    database.parent.mkdir(parents=True)
    expected = {
        "index.db": b"database-bytes",
        "index.db-wal": b"wal-bytes",
        "index.db-shm": b"shm-bytes",
        "index.db-journal": b"journal-bytes",
    }
    for name, payload in expected.items():
        (database.parent / name).write_bytes(payload)

    backup_dir, copied = index_recovery._backup_index_files(database)

    assert {path.name for path in copied} == set(expected)
    assert {
        name: (backup_dir / name).read_bytes() for name in expected
    } == expected
    assert {
        name: (database.parent / name).read_bytes() for name in expected
    } == expected


def test_guided_rebuild_restores_readable_user_state(recovery_install):
    conn = index_module.open_index()
    try:
        index_module.upsert_sessions(conn, [_session()])
        index_module.update_session(
            conn,
            "session-recovery",
            status="approved",
            notes="Keep this review note",
            reason="Useful recovery trace",
        )
        index_module.set_hold_state(
            conn,
            "session-recovery",
            "pending_review",
            changed_by="user",
            reason="Do not share yet",
        )
        policy_id = index_module.add_policy(
            conn,
            "redact_string",
            "private-recovery-value",
            reason="User-authored rule",
        )
    finally:
        conn.close()

    result = index_recovery.guided_rebuild(_scan_one_session)

    assert result["status"] == "ready"
    backup_dir = Path(result["backup_path"])
    assert (backup_dir / "index.db").is_file()
    manifest = json.loads((backup_dir / "recovery-manifest.json").read_text())
    assert manifest["unreadable_state"] == []
    assert not (recovery_install / index_recovery.RECOVERY_MARKER_FILENAME).exists()

    rebuilt = index_module.open_index()
    try:
        row = rebuilt.execute(
            "SELECT review_status, reviewer_notes, selection_reason, hold_state "
            "FROM sessions WHERE session_id = ?",
            ("session-recovery",),
        ).fetchone()
        assert dict(row) == {
            "review_status": "approved",
            "reviewer_notes": "Keep this review note",
            "selection_reason": "Useful recovery trace",
            "hold_state": "pending_review",
        }
        assert rebuilt.execute(
            "SELECT COUNT(*) FROM session_hold_history WHERE session_id = ?",
            ("session-recovery",),
        ).fetchone()[0] >= 2
        assert rebuilt.execute(
            "SELECT value FROM policies WHERE policy_id = ?", (policy_id,)
        ).fetchone()[0] == "private-recovery-value"
    finally:
        rebuilt.close()


@pytest.mark.parametrize("damaged_hold", [None, "", "unknown-state"])
def test_rebuild_fails_closed_for_invalid_hold_state(
    recovery_install,
    damaged_hold,
):
    conn = index_module.open_index()
    try:
        index_module.upsert_sessions(conn, [_session()])
        index_module.update_session(
            conn,
            "session-recovery",
            status="approved",
            notes="Preserve this non-safety note",
        )
        conn.execute(
            "UPDATE sessions SET hold_state = ? WHERE session_id = ?",
            (damaged_hold, "session-recovery"),
        )
        conn.commit()
    finally:
        conn.close()

    result = index_recovery.guided_rebuild(_scan_one_session)

    assert any("require review" in item for item in result["warnings"])
    rebuilt = index_module.open_index()
    try:
        row = rebuilt.execute(
            "SELECT review_status, reviewer_notes, hold_state, embargo_until "
            "FROM sessions WHERE session_id = ?",
            ("session-recovery",),
        ).fetchone()
        assert tuple(row) == (
            "new",
            "Preserve this non-safety note",
            "pending_review",
            None,
        )
    finally:
        rebuilt.close()


def test_rebuild_restores_share_and_findings_but_pauses_automatic_uploads(
    recovery_install,
):
    conn = index_module.open_index()
    try:
        index_module.upsert_sessions(conn, [_session()])
        index_module.update_session(conn, "session-recovery", status="approved")
        _add_recovery_finding(conn, decided=True)
        share_id = index_module.create_share(conn, ["session-recovery"])
        conn.execute(
            "UPDATE shares SET status = 'shared', shared_at = ? WHERE share_id = ?",
            ("2026-07-30T00:30:00+00:00", share_id),
        )
        conn.commit()
        index_module.save_auto_upload_enrollment(
            conn,
            mode="enabled",
            health="ready",
            generation=1,
            enrolled_at="2026-07-30T00:00:00+00:00",
            client_enrollment_id="client-recovery",
            enrolled_sources=("claude",),
            enrolled_projects=("recovery-project",),
            server_enrollment_id="server-recovery",
            authorization_revision=1,
            recurring_authorization_version="recurring-v2",
            retention_version="retention-v1",
            ownership_certification_version="ownership-v1",
            server_scope_hash="scope-hash",
            egress_profile_hash="profile-hash",
            current_run_id="run-before-corruption",
            current_run_stage="scanning",
            next_retry_at="2026-07-31T00:00:00+00:00",
        )
        index_module.save_auto_upload_enrollment_job(
            conn,
            job_id="job-before-corruption",
            state="queued",
            request={"source": "claude"},
            current_stage="queued",
        )
    finally:
        conn.close()

    result = index_recovery.guided_rebuild(_scan_one_session_with_finding)

    assert any("Automatic uploads were restored paused" in item for item in result["warnings"])
    assert any("setup was left in the backup" in item for item in result["warnings"])
    rebuilt = index_module.open_index()
    try:
        enrollment = index_module.get_auto_upload_enrollment(rebuilt)
        assert enrollment is not None
        assert enrollment["mode"] == "paused"
        assert enrollment["health"] == "action_required"
        assert enrollment["current_run_id"] is None
        assert enrollment["current_run_stage"] is None
        assert enrollment["next_retry_at"] is None
        assert enrollment["last_result_code"] == "index_recovery_review_required"
        assert index_module.get_auto_upload_enrollment_job(rebuilt) is None
        share = rebuilt.execute(
            "SELECT status, shared_at FROM shares WHERE share_id = ?",
            (share_id,),
        ).fetchone()
        assert tuple(share) == ("shared", "2026-07-30T00:30:00+00:00")
        assert rebuilt.execute(
            "SELECT COUNT(*) FROM share_sessions WHERE share_id = ?",
            (share_id,),
        ).fetchone()[0] == 1
        finding = rebuilt.execute(
            "SELECT status, decided_by, decision_reason FROM findings "
            "WHERE finding_id = 'finding-recovery'"
        ).fetchone()
        assert tuple(finding) == ("ignored", "user", "Known test value")
    finally:
        rebuilt.close()


def test_unreadable_state_rebuild_marks_every_trace_pending_review(
    recovery_install,
):
    database = recovery_install / "index.db"
    database.parent.mkdir(parents=True)
    damaged_bytes = b"this is not sqlite"
    database.write_bytes(damaged_bytes)

    result = index_recovery.guided_rebuild(_scan_one_session)

    assert result["status"] == "ready"
    assert any("requires review" in warning for warning in result["warnings"])
    backup_dir = Path(result["backup_path"])
    assert (backup_dir / "index.db").read_bytes() == damaged_bytes

    conn = index_module.open_index()
    try:
        row = conn.execute(
            "SELECT review_status, hold_state, embargo_until FROM sessions"
        ).fetchone()
        assert dict(row) == {
            "review_status": "new",
            "hold_state": "pending_review",
            "embargo_until": None,
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_foreign_key_damage_is_rebuilt_without_orphan_rows(recovery_install):
    database = recovery_install / "index.db"
    conn = index_module.open_index()
    try:
        index_module.upsert_sessions(conn, [_session()])
        index_module.update_session(conn, "session-recovery", status="approved")
    finally:
        conn.close()

    damaged = sqlite3.connect(database)
    try:
        damaged.execute("PRAGMA foreign_keys=OFF")
        damaged.execute(
            """INSERT INTO session_hold_history (
                history_id, session_id, from_state, to_state,
                changed_by, changed_at
            ) VALUES ('orphan-history', 'missing-session', NULL,
                      'pending_review', 'test', '2026-07-30T00:00:00+00:00')"""
        )
        damaged.execute(
            """INSERT INTO share_sessions (
                share_id, session_id, added_at
            ) VALUES ('missing-share', 'missing-session',
                      '2026-07-30T00:00:00+00:00')"""
        )
        damaged.execute(
            "UPDATE sessions SET share_id = 'missing-share' "
            "WHERE session_id = 'session-recovery'"
        )
        damaged.commit()
    finally:
        damaged.close()

    report = index_recovery.inspect_index_health()
    assert report["status"] == "recovery_required"
    assert "relational safety state" in report["unreadable_state"]

    result = index_recovery.guided_rebuild(_scan_one_session)

    assert any("orphaned hold-history" in item for item in result["warnings"])
    assert any("orphaned share link" in item for item in result["warnings"])
    assert any("orphaned session share link" in item for item in result["warnings"])
    assert any("every rebuilt trace" in item for item in result["warnings"])
    rebuilt = index_module.open_index()
    try:
        assert rebuilt.execute("PRAGMA foreign_key_check").fetchall() == []
        assert rebuilt.execute(
            "SELECT hold_state FROM sessions WHERE session_id = ?",
            ("session-recovery",),
        ).fetchone()[0] == "pending_review"
    finally:
        rebuilt.close()


def test_failed_rebuild_marker_survives_and_retry_uses_original_backup(
    recovery_install,
):
    database = recovery_install / "index.db"
    database.parent.mkdir(parents=True)
    damaged_bytes = b"unreadable original index"
    database.write_bytes(damaged_bytes)

    with pytest.raises(RuntimeError, match="rescan did not complete"):
        index_recovery.guided_rebuild(lambda: {"ok": False})

    with pytest.raises(sqlite3.DatabaseError, match="unfinished recovery"):
        index_module.open_index()

    marker_path = recovery_install / index_recovery.RECOVERY_MARKER_FILENAME
    marker = json.loads(marker_path.read_text())
    backup_dir = Path(marker["backup_path"])
    assert marker["stage"] == "failed"
    assert (backup_dir / "index.db").read_bytes() == damaged_bytes
    assert index_recovery.inspect_index_health()["interrupted_recovery"] is True

    result = index_recovery.guided_rebuild(_scan_one_session)

    assert result["status"] == "ready"
    assert Path(result["backup_path"]) == backup_dir
    assert not marker_path.exists()
    conn = index_module.open_index()
    try:
        assert conn.execute(
            "SELECT hold_state FROM sessions WHERE session_id = ?",
            ("session-recovery",),
        ).fetchone()[0] == "pending_review"
    finally:
        conn.close()
