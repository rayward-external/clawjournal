"""Doctor probe tests — five-branch install detection + verdicts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from clawjournal.events.doctor import overlay as overlay_mod
from clawjournal.events.doctor import probes
from clawjournal.events.schema import ensure_schema as ensure_events_schema
from clawjournal.workbench import index as index_module
from clawjournal.workbench.index import open_index


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "clawjournal.workbench.index.INDEX_DB", tmp_path / ".clawjournal" / "index.db"
    )
    monkeypatch.setattr(
        "clawjournal.workbench.index.CONFIG_DIR", tmp_path / ".clawjournal"
    )
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
    overlay_mod.reset_cache()
    yield
    overlay_mod.reset_cache()


def _make_workbench_only(tmp_path: Path) -> None:
    cfg = tmp_path / ".clawjournal"
    cfg.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg / "index.db")
    try:
        # Workbench schema only — no events tables.
        conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()


def test_paths_follow_configured_state_root(monkeypatch, tmp_path):
    state_root = tmp_path / "relocated-state"
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", state_root)

    assert probes.config_dir() == state_root
    assert probes.index_db_path() == state_root / "index.db"


def test_readonly_probe_honors_recovery_marker_for_relocated_special_path(tmp_path):
    database = tmp_path / "state %23#relocated" / "index.db"
    database.parent.mkdir()
    conn = sqlite3.connect(database)
    try:
        conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    probe = probes._open_readonly(database)
    assert probe is not None
    probes._close_readonly(probe)

    index_module._index_recovery_marker_path(database).write_text(
        "{}",
        encoding="utf-8",
    )
    assert probes._open_readonly(database) is None


def test_fresh_install_returns_zero(monkeypatch, tmp_path):
    # No ~/.clawjournal/ exists — fresh state.
    report = probes.collect()
    assert report.install_state == probes.INSTALL_FRESH
    assert probes.exit_code_for(report) == 0


def test_db_missing_returns_three(monkeypatch, tmp_path):
    (tmp_path / ".clawjournal").mkdir()
    report = probes.collect()
    assert report.install_state == probes.INSTALL_DB_MISSING
    assert probes.exit_code_for(report) == 3


def test_workbench_only_returns_one(monkeypatch, tmp_path):
    _make_workbench_only(tmp_path)
    report = probes.collect()
    assert report.install_state == probes.INSTALL_WORKBENCH_ONLY
    assert probes.exit_code_for(report) == 1


def test_events_empty_returns_zero(monkeypatch, tmp_path):
    cfg = tmp_path / ".clawjournal"
    cfg.mkdir(parents=True)
    conn = open_index()
    try:
        ensure_events_schema(conn)
    finally:
        conn.close()
    report = probes.collect()
    assert report.install_state == probes.INSTALL_EVENTS_EMPTY
    assert probes.exit_code_for(report) == 0


def test_db_corrupt_returns_five(monkeypatch, tmp_path):
    cfg = tmp_path / ".clawjournal"
    cfg.mkdir(parents=True)
    # Write a non-SQLite file at index.db
    (cfg / "index.db").write_bytes(b"not a sqlite file at all")
    report = probes.collect()
    assert report.install_state == probes.INSTALL_DB_CORRUPT
    assert probes.exit_code_for(report) == 5


def test_empty_db_file_is_corrupt(monkeypatch, tmp_path):
    """A 0-byte index.db opens cleanly in SQLite RO mode but has no
    schema. Doctor must classify it as corrupt, not healthy."""
    cfg = tmp_path / ".clawjournal"
    cfg.mkdir(parents=True)
    (cfg / "index.db").write_bytes(b"")
    report = probes.collect()
    assert report.install_state == probes.INSTALL_DB_CORRUPT
    assert probes.exit_code_for(report) == 5


def test_unknown_event_type_triggers_unknown_schema(monkeypatch, tmp_path):
    cfg = tmp_path / ".clawjournal"
    cfg.mkdir(parents=True)
    conn = open_index()
    try:
        ensure_events_schema(conn)
        conn.execute(
            "INSERT INTO event_sessions (session_key, client, client_version, "
            "started_at, status) VALUES (?, ?, ?, ?, ?)",
            ("claude:test:abc", "claude", "1.45.0-rc.1", "2026-01-01T00:00:00Z", "ended"),
        )
        sid = conn.execute("SELECT id FROM event_sessions").fetchone()[0]
        conn.execute(
            "INSERT INTO events (session_id, ingested_at, source, source_path, "
            "source_offset, seq, type, raw_json, event_at, confidence, lossiness, client) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                "2026-01-01T00:00:00Z",
                "claude-jsonl",
                "/tmp/x.jsonl",
                0,
                0,
                "tool_call_v2",
                "{}",
                "2026-01-01T00:00:00Z",
                "high",
                "none",
                "claude",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    report = probes.collect()
    assert any(c.verdict == probes.VERDICT_UNKNOWN_SCHEMA for c in report.clients)
    assert probes.exit_code_for(report) == 6


def test_schema_unknown_row_triggers_partial(monkeypatch, tmp_path):
    cfg = tmp_path / ".clawjournal"
    cfg.mkdir(parents=True)
    conn = open_index()
    try:
        ensure_events_schema(conn)
        conn.execute(
            "INSERT INTO event_sessions (session_key, client, client_version, "
            "started_at, status) VALUES (?, ?, ?, ?, ?)",
            ("claude:test:abc", "claude", "1.43.0", "2026-01-01T00:00:00Z", "ended"),
        )
        sid = conn.execute("SELECT id FROM event_sessions").fetchone()[0]
        conn.execute(
            "INSERT INTO events (session_id, ingested_at, source, source_path, "
            "source_offset, seq, type, raw_json, event_at, confidence, lossiness, client) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                "2026-01-01T00:00:00Z",
                "claude-jsonl",
                "/tmp/x.jsonl",
                0,
                0,
                "schema_unknown",
                "{}",
                "2026-01-01T00:00:00Z",
                "high",
                "partial",
                "claude",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    report = probes.collect()
    assert any(c.verdict == probes.VERDICT_PARTIAL for c in report.clients)
    assert probes.exit_code_for(report) == 1
