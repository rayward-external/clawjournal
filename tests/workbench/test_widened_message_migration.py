"""Tests for the widened-message-model schema migration (phase-2 C1).

Bumps PRAGMA user_version 3 → 4 and adds the
``sessions.message_schema_version`` column. Existing rows are stamped
with version 1 (legacy shape); fresh inserts default to version 2
(widened shape). The column is a forward-compat marker — current
readers do not consult it.
"""

import sqlite3

import pytest

from clawjournal.workbench.index import (
    HOSTED_SUBMISSION_SCHEMA_VERSION,
    SESSION_IDENTITY_SCHEMA_VERSION,
    WIDENED_MESSAGE_SCHEMA_VERSION,
    WORKBENCH_SCHEMA_VERSION,
    open_index,
)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "index.db"
    blobs_path = tmp_path / "blobs"
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", db_path)
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", blobs_path)
    return db_path


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


class TestFreshInstall:
    def test_workbench_version_advances_to_widened(self, fresh_db):
        assert WORKBENCH_SCHEMA_VERSION == HOSTED_SUBMISSION_SCHEMA_VERSION
        assert HOSTED_SUBMISSION_SCHEMA_VERSION > WIDENED_MESSAGE_SCHEMA_VERSION
        assert WIDENED_MESSAGE_SCHEMA_VERSION > SESSION_IDENTITY_SCHEMA_VERSION
        conn = open_index()
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == WORKBENCH_SCHEMA_VERSION
        finally:
            conn.close()

    def test_message_schema_version_column_exists(self, fresh_db):
        conn = open_index()
        try:
            cols = _columns(conn, "sessions")
            assert "message_schema_version" in cols
        finally:
            conn.close()


class TestUpgradeFromV3:
    def _downgrade_to_v3(self, db_path):
        """Roll a v4 DB back to v3 state: drop the new column + reset version.

        Rather than constructing a v3 schema from scratch (which would
        require reproducing every ALTER TABLE addition the v0→v3 path
        applies), open a fresh DB at v4, then surgically remove the v4
        artifacts. SQLite 3.35+ supports ``DROP COLUMN`` directly.
        """
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("ALTER TABLE sessions DROP COLUMN message_schema_version")
        conn.execute(f"PRAGMA user_version = {SESSION_IDENTITY_SCHEMA_VERSION}")
        conn.commit()
        conn.close()

    def test_upgrade_adds_column_and_backfills_legacy(self, fresh_db):
        # Step 1: fresh install lands at the current version with the column.
        conn = open_index()
        conn.execute(
            "INSERT INTO sessions (session_id, project, source, indexed_at) "
            "VALUES ('legacy-1', 'p', 'claude', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()

        # Step 2: roll back to v3 to simulate an existing user upgrading.
        self._downgrade_to_v3(fresh_db)

        # Step 3: re-open. The migration should run, add the column,
        # and stamp the pre-existing row as legacy (1).
        conn = open_index()
        try:
            cols = _columns(conn, "sessions")
            assert "message_schema_version" in cols
            row = conn.execute(
                "SELECT message_schema_version FROM sessions WHERE session_id='legacy-1'"
            ).fetchone()
            assert row[0] == 1
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == WORKBENCH_SCHEMA_VERSION
        finally:
            conn.close()

    def test_upgrade_is_idempotent(self, fresh_db):
        # Open twice from a fresh DB — second open is a no-op for all
        # gated migrations since user_version is already current.
        conn = open_index()
        conn.execute(
            "INSERT INTO sessions (session_id, project, source, indexed_at) "
            "VALUES ('s1', 'p', 'claude', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        version_before = conn.execute("PRAGMA user_version").fetchone()[0]
        msv_before = conn.execute(
            "SELECT message_schema_version FROM sessions WHERE session_id='s1'"
        ).fetchone()[0]
        conn.close()

        conn = open_index()
        try:
            version_after = conn.execute("PRAGMA user_version").fetchone()[0]
            msv_after = conn.execute(
                "SELECT message_schema_version FROM sessions WHERE session_id='s1'"
            ).fetchone()[0]
            assert version_after == version_before == WORKBENCH_SCHEMA_VERSION
            # A second open must not flip a widened row back to legacy.
            assert msv_after == msv_before == 2
        finally:
            conn.close()

    def test_fresh_install_inserts_default_to_widened(self, fresh_db):
        """New rows on a fresh DB get version 2 (widened) via column default."""
        conn = open_index()
        try:
            conn.execute(
                "INSERT INTO sessions (session_id, project, source, indexed_at) "
                "VALUES ('new-1', 'p', 'claude', '2026-01-01T00:00:00+00:00')"
            )
            conn.commit()
            row = conn.execute(
                "SELECT message_schema_version FROM sessions WHERE session_id='new-1'"
            ).fetchone()
            assert row[0] == 2
        finally:
            conn.close()


class TestUpgradeFromV4:
    """The v4 → v5 migration adds `shares.hosted_receipt_id` and friends.

    These tests start from a fresh v5 DB, roll it back to v4 by surgically
    removing the new columns, then re-open via ``open_index`` to confirm
    the migration re-adds them and advances ``PRAGMA user_version``.
    """

    def _downgrade_to_v4(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=OFF")
        for col in ("hosted_receipt_id", "hosted_status", "hosted_submission_url"):
            conn.execute(f"ALTER TABLE shares DROP COLUMN {col}")
        conn.execute(f"PRAGMA user_version = {WIDENED_MESSAGE_SCHEMA_VERSION}")
        conn.commit()
        conn.close()

    def test_fresh_install_has_hosted_submission_columns(self, fresh_db):
        conn = open_index()
        try:
            cols = _columns(conn, "shares")
            assert {"hosted_receipt_id", "hosted_status", "hosted_submission_url"}.issubset(cols)
        finally:
            conn.close()

    def test_upgrade_adds_hosted_submission_columns(self, fresh_db):
        # Land at v5, insert a pre-existing share, then roll back to v4.
        conn = open_index()
        conn.execute(
            "INSERT INTO shares (share_id, created_at, status) "
            "VALUES ('share-legacy', '2026-01-01T00:00:00+00:00', 'exported')"
        )
        conn.commit()
        conn.close()

        self._downgrade_to_v4(fresh_db)

        with sqlite3.connect(str(fresh_db)) as raw:
            cols_before = _columns(raw, "shares")
            assert "hosted_receipt_id" not in cols_before
            version_before = raw.execute("PRAGMA user_version").fetchone()[0]
            assert version_before == WIDENED_MESSAGE_SCHEMA_VERSION

        conn = open_index()
        try:
            cols_after = _columns(conn, "shares")
            assert {"hosted_receipt_id", "hosted_status", "hosted_submission_url"}.issubset(cols_after)
            version_after = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version_after == HOSTED_SUBMISSION_SCHEMA_VERSION
            # Pre-existing rows keep their data; the new columns default to NULL.
            row = conn.execute(
                "SELECT hosted_receipt_id, hosted_status, hosted_submission_url "
                "FROM shares WHERE share_id = 'share-legacy'"
            ).fetchone()
            assert tuple(row) == (None, None, None)
        finally:
            conn.close()

    def test_upgrade_is_idempotent(self, fresh_db):
        # Open twice from a fresh DB — second open is a no-op for the
        # hosted-submission migration since user_version already == v5.
        conn = open_index()
        version_before = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()

        conn = open_index()
        try:
            version_after = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version_after == version_before == HOSTED_SUBMISSION_SCHEMA_VERSION
            cols = _columns(conn, "shares")
            assert {"hosted_receipt_id", "hosted_status", "hosted_submission_url"}.issubset(cols)
        finally:
            conn.close()
