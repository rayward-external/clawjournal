from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from clawjournal import cli
from clawjournal import filesystem
from clawjournal import selfupdate
from clawjournal import support_diagnostics as diagnostics


def _create_index(path: Path, *, violations: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=delete")
        conn.execute("PRAGMA user_version=12")
        conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE share_sessions ("
            "share_id TEXT PRIMARY KEY, "
            "session_id TEXT REFERENCES sessions(session_id))"
        )
        for number in range(violations):
            conn.execute(
                "INSERT INTO share_sessions(share_id, session_id) VALUES (?, ?)",
                (f"share-{number}", f"missing-{number}"),
            )
        conn.commit()
    finally:
        conn.close()


def _healthy_report() -> dict[str, object]:
    return {
        "support_diagnostics_schema_version": 1,
        "kind": "index",
        "package": {"version": "0.2.0", "revision": "a" * 40},
        "runtime": {"python_version": "3.13.0", "sqlite_version": "3.49.0"},
        "schema": {"expected_user_version": 12},
        "storage": {
            "filesystem_type": "ext4",
            "storage_risk": "local",
            "storage_migration_required": False,
        },
        "index": {
            "exists": True,
            "health_code": "healthy",
            "user_version": 12,
            "journal_mode": "delete",
            "quick_check": {"status": "ok", "issue_count": 0, "truncated": False},
            "foreign_key_check": {
                "status": "ok",
                "returned_count": 0,
                "truncated": False,
                "violations": [],
            },
        },
    }


def test_version_string_includes_valid_checkout_revision(monkeypatch):
    monkeypatch.setattr(diagnostics, "package_revision", lambda: "a" * 40)
    assert diagnostics.version_string() == "clawjournal 0.2.0 (aaaaaaa)"


def test_invalid_git_output_is_not_exposed(monkeypatch, tmp_path):
    class Result:
        returncode = 0
        stdout = b"/home/alice/private\n"

    monkeypatch.setattr(diagnostics, "_checkout_root", lambda: tmp_path)
    monkeypatch.setattr(diagnostics.subprocess, "run", lambda *args, **kwargs: Result())
    assert diagnostics.package_revision() is None


def test_global_version_is_quiet_and_does_not_selfupdate(monkeypatch, capsys):
    monkeypatch.setattr(
        selfupdate,
        "maybe_self_update",
        lambda: pytest.fail("--version must not trigger selfupdate"),
    )
    monkeypatch.setattr(diagnostics, "package_revision", lambda: "b" * 40)
    monkeypatch.setattr(sys, "argv", ["clawjournal", "--version"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == "clawjournal 0.2.0 (bbbbbbb)\n"
    assert captured.err == ""


def test_top_level_doctor_is_read_only_with_respect_to_selfupdate():
    assert diagnostics is not None
    assert cli._should_auto_update(["clawjournal", "doctor", "index", "--json"]) is False


def test_network_storage_refusal_has_one_line_cli_error(monkeypatch, capsys):
    from clawjournal.workbench.index import UnsafeIndexStorageError

    def refuse_scan(*args, **kwargs):
        raise UnsafeIndexStorageError(filesystem.FilesystemInfo("nfs", "network"))

    monkeypatch.setattr(cli, "_run_scan", refuse_scan)
    monkeypatch.setattr(cli, "_should_auto_update", lambda argv=None: False)
    monkeypatch.setattr(sys, "argv", ["clawjournal", "scan"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ClawJournal's state directory")
    assert captured.err.count("\n") == 1
    assert "Traceback" not in captured.err
    assert "CLAWJOURNAL_HOME" in captured.err


def test_healthy_index_report_is_allowlisted_and_contains_no_path(
    monkeypatch, tmp_path
):
    state_dir = tmp_path / "private" / "alice-at-purdue"
    _create_index(state_dir / "index.db")
    monkeypatch.setattr(diagnostics, "_expected_schema_version", lambda: 12)
    monkeypatch.setattr(diagnostics, "package_revision", lambda: "c" * 40)
    monkeypatch.setattr(
        filesystem,
        "classify_filesystem",
        lambda path: filesystem.FilesystemInfo("ext4", "local"),
    )
    before_files = sorted(path.name for path in state_dir.iterdir())
    before_bytes = (state_dir / "index.db").read_bytes()

    report = diagnostics.collect_index_diagnostics(state_dir=state_dir)

    assert set(report) == {
        "support_diagnostics_schema_version",
        "kind",
        "package",
        "runtime",
        "schema",
        "storage",
        "index",
    }
    assert report["storage"] == {
        "filesystem_type": "ext4",
        "storage_risk": "local",
        "storage_migration_required": False,
    }
    assert set(report["runtime"]) == {
        "python_version",
        "sqlite_version",
        "os_family",
        "os_release",
        "architecture",
    }
    assert report["index"]["health_code"] == "healthy"
    assert report["index"]["quick_check"]["status"] == "ok"
    assert report["index"]["foreign_key_check"]["status"] == "ok"
    assert diagnostics.diagnostics_exit_code(report) == 0
    assert sorted(path.name for path in state_dir.iterdir()) == before_files
    assert (state_dir / "index.db").read_bytes() == before_bytes
    assert str(tmp_path) not in json.dumps(report)
    rendered = diagnostics.render_index_diagnostics(report)
    assert "alice-at-purdue" not in rendered
    assert "\r" not in rendered


def test_network_storage_is_reported_without_mount_source(monkeypatch, tmp_path):
    _create_index(tmp_path / "index.db")
    monkeypatch.setattr(diagnostics, "_expected_schema_version", lambda: 12)
    monkeypatch.setattr(
        filesystem,
        "classify_filesystem",
        lambda path: filesystem.FilesystemInfo("nfs", "network"),
    )

    report = diagnostics.collect_index_diagnostics(state_dir=tmp_path)

    assert report["storage"] == {
        "filesystem_type": "nfs",
        "storage_risk": "network",
        "storage_migration_required": True,
    }
    assert report["index"]["exists"] is None
    assert report["index"]["health_code"] == "network_storage_not_inspected"
    assert diagnostics.diagnostics_exit_code(report) == 1
    serialized = json.dumps(report)
    assert str(tmp_path) not in serialized
    assert "server:" not in serialized


def test_network_storage_never_touches_the_index(monkeypatch, tmp_path):
    monkeypatch.setattr(
        filesystem,
        "classify_filesystem",
        lambda path: filesystem.FilesystemInfo("nfs4", "network"),
    )
    monkeypatch.setattr(
        diagnostics,
        "_index_report",
        lambda *_args, **_kwargs: pytest.fail(
            "network storage must not be statted or opened"
        ),
    )

    report = diagnostics.collect_index_diagnostics(
        state_dir=tmp_path / "disconnected-network-home"
    )

    assert report["index"]["health_code"] == "network_storage_not_inspected"


def test_live_wal_snapshot_is_reported_without_opening_or_mutating_it(
    monkeypatch,
    tmp_path,
):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    database = state_dir / "index.db"
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("PRAGMA journal_mode=wal").fetchone()[0] == "wal"
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("PRAGMA user_version=12")
        conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO sessions VALUES ('uncheckpointed')")
        conn.commit()
        before = {
            path.name: path.read_bytes()
            for path in state_dir.iterdir()
            if path.is_file()
        }
        assert "index.db-wal" in before
        monkeypatch.setattr(diagnostics, "_expected_schema_version", lambda: 12)
        monkeypatch.setattr(
            filesystem,
            "classify_filesystem",
            lambda path: filesystem.FilesystemInfo("ext4", "local"),
        )

        report = diagnostics.collect_index_diagnostics(state_dir=state_dir)

        after = {
            path.name: path.read_bytes()
            for path in state_dir.iterdir()
            if path.is_file()
        }
        assert report["index"]["health_code"] == (
            "sidecar_snapshot_not_inspected"
        )
        assert report["index"]["quick_check"]["status"] == "not_run"
        assert report["index"]["foreign_key_check"]["status"] == "not_run"
        assert after == before
    finally:
        conn.close()


def test_closed_wal_header_is_not_misreported_as_delete(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    database = state_dir / "index.db"
    state_dir.mkdir()
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("PRAGMA journal_mode=wal").fetchone()[0] == "wal"
        conn.execute("PRAGMA user_version=12")
        conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    assert diagnostics._header_journal_mode(database) == "wal"
    assert not Path(str(database) + "-wal").exists()
    monkeypatch.setattr(diagnostics, "_expected_schema_version", lambda: 12)
    monkeypatch.setattr(
        filesystem,
        "classify_filesystem",
        lambda path: filesystem.FilesystemInfo("ext4", "local"),
    )

    report = diagnostics.collect_index_diagnostics(state_dir=state_dir)

    assert report["index"]["journal_mode"] == "wal"
    assert report["index"]["health_code"] == "unexpected_journal_mode"


def test_runtime_canary_cannot_leak_username_or_path(monkeypatch, tmp_path):
    private_marker = "alice-private-state"
    state_dir = tmp_path / private_marker
    monkeypatch.setattr(
        diagnostics.platform,
        "system",
        lambda: f"Linux {private_marker}",
    )
    monkeypatch.setattr(
        diagnostics.platform,
        "release",
        lambda: f"6.8.0 /home/{private_marker}",
    )
    monkeypatch.setattr(
        diagnostics.platform,
        "machine",
        lambda: f"x86_64-{private_marker}",
    )
    monkeypatch.setattr(
        filesystem,
        "classify_filesystem",
        lambda path: filesystem.FilesystemInfo("nfs", "network"),
    )

    report = diagnostics.collect_index_diagnostics(state_dir=state_dir)
    serialized = json.dumps(report)

    assert report["runtime"] == {
        "python_version": diagnostics.platform.python_version(),
        "sqlite_version": sqlite3.sqlite_version,
        "os_family": "unknown",
        "os_release": "6.8.0",
        "architecture": "unknown",
    }
    assert private_marker not in serialized
    assert "/home/" not in serialized


def test_user_chosen_fuse_subtype_is_never_returned(monkeypatch, tmp_path):
    private_subtype = "fuse.alice_private_project"
    monkeypatch.setattr(
        filesystem,
        "classify_filesystem",
        lambda path: filesystem.FilesystemInfo(private_subtype, "unknown"),
    )

    report = diagnostics.collect_index_diagnostics(state_dir=tmp_path)
    serialized = json.dumps(report)

    assert report["storage"]["filesystem_type"] == "unknown"
    assert private_subtype not in serialized


def test_foreign_key_violations_are_bounded(monkeypatch, tmp_path):
    _create_index(tmp_path / "index.db", violations=25)
    monkeypatch.setattr(diagnostics, "_expected_schema_version", lambda: 12)

    report = diagnostics.collect_index_diagnostics(state_dir=tmp_path)
    check = report["index"]["foreign_key_check"]

    assert report["index"]["health_code"] == "foreign_key_violations"
    assert check["status"] == "violations"
    assert check["returned_count"] == diagnostics.MAX_FOREIGN_KEY_VIOLATIONS
    assert check["truncated"] is True
    assert len(check["violations"]) == diagnostics.MAX_FOREIGN_KEY_VIOLATIONS
    assert check["violations"][0] == {
        "table": "share_sessions",
        "row_id": 1,
        "parent": "sessions",
        "foreign_key_id": 0,
    }


def test_unknown_schema_identifiers_are_never_returned():
    assert diagnostics._safe_schema_identifier("sessions") == "sessions"
    assert diagnostics._safe_schema_identifier("alice_private_table") == "redacted"


def test_quick_check_never_returns_raw_database_messages():
    class Cursor:
        def fetchmany(self, _limit):
            return [("failure mentions /home/alice/private-trace",)]

    class Connection:
        def execute(self, _query):
            return Cursor()

    result = diagnostics._run_quick_check(Connection())
    assert result == {"status": "failed", "issue_count": 1, "truncated": False}
    assert "alice" not in json.dumps(result)


def test_corrupt_and_missing_indexes_return_partial_reports(monkeypatch, tmp_path):
    monkeypatch.setattr(diagnostics, "_expected_schema_version", lambda: 12)
    missing = diagnostics.collect_index_diagnostics(state_dir=tmp_path / "missing")
    assert missing["index"]["health_code"] == "missing"
    assert diagnostics.diagnostics_exit_code(missing) == 1
    assert not (tmp_path / "missing").exists()

    state_dir = tmp_path / "corrupt"
    state_dir.mkdir()
    (state_dir / "index.db").write_bytes(b"not a sqlite database; /home/alice")
    corrupt = diagnostics.collect_index_diagnostics(state_dir=state_dir)
    assert corrupt["index"]["health_code"] == "corrupt"
    assert str(tmp_path) not in json.dumps(corrupt)
    assert "alice" not in json.dumps(corrupt)


def test_doctor_index_json_uses_machine_readable_payload(monkeypatch, capsys):
    report = _healthy_report()
    monkeypatch.setattr(diagnostics, "collect_index_diagnostics", lambda: report)
    monkeypatch.setattr(sys, "argv", ["clawjournal", "doctor", "index", "--json"])

    cli.main()

    captured = capsys.readouterr()
    assert json.loads(captured.out) == report
    assert captured.err == ""


def test_human_diagnostics_use_portable_newlines():
    rendered = diagnostics.render_index_diagnostics(_healthy_report())

    assert "\r" not in rendered
    assert rendered.count("\n") == 5
