"""Manifest invariants: sha256 reproducibility, schema versions, format."""

from __future__ import annotations

import json

import pytest

from clawjournal.events.export import (
    BUNDLE_SCHEMA_VERSION,
    RECORDER_SCHEMA_VERSION,
    ExportGateBlocked,
    export_session_bundle,
)

from ._helpers import (
    PERMISSIVE_CONFIG,
    insert_event,
    insert_event_session,
    insert_workbench_session,
    make_conn,
)


def _export(conn, key, tmp_path, monkeypatch):
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
    return export_session_bundle(
        conn,
        key,
        config=PERMISSIVE_CONFIG,
        allow_no_workbench_row=True,
        skip_global_gates=True,
    )


@pytest.fixture
def populated_conn():
    conn = make_conn()
    sid = insert_event_session(conn, session_key="claude:p:s")
    insert_event(
        conn,
        session_id=sid,
        event_type="user_message",
        source_path="/tmp/x.jsonl",
        source_offset=0,
        seq=0,
        raw_json={"text": "hello"},
    )
    insert_event(
        conn,
        session_id=sid,
        event_type="assistant_message",
        source_path="/tmp/x.jsonl",
        source_offset=80,
        seq=0,
        event_at="2026-04-22T09:01:01Z",
        raw_json={"text": "world"},
    )
    return conn


def test_sha256_is_reproducible(populated_conn, tmp_path, monkeypatch):
    """For fixed input (including bundle_created_at), the sha256 is stable.
    The wall-clock-driven `bundle_created_at` is what makes two real
    exports differ — see test_manifest_excludes_itself_from_sha_input."""
    monkeypatch.setattr(
        "clawjournal.events.export.bundle._utc_now",
        lambda: "2026-04-23T17:00:00Z",
    )
    s1 = _export(populated_conn, "claude:p:s", tmp_path, monkeypatch)
    s2 = _export(populated_conn, "claude:p:s", tmp_path, monkeypatch)
    assert s1.sha256 == s2.sha256


def test_schema_version_pinned(populated_conn, tmp_path, monkeypatch):
    summary = _export(populated_conn, "claude:p:s", tmp_path, monkeypatch)
    bundle = json.loads(summary.bundle_path.read_text(encoding="utf-8"))
    assert bundle["bundle_schema_version"] == BUNDLE_SCHEMA_VERSION == "1.0"
    assert bundle["recorder_schema_version"] == RECORDER_SCHEMA_VERSION == "1.0"


def test_capabilities_snapshot_present(populated_conn, tmp_path, monkeypatch):
    summary = _export(populated_conn, "claude:p:s", tmp_path, monkeypatch)
    bundle = json.loads(summary.bundle_path.read_text(encoding="utf-8"))
    assert "capabilities" in bundle
    # Capability keys are stringified `client:event_type` pairs;
    # capabilities_json() returns the matrix in dict shape.
    assert isinstance(bundle["capabilities"], dict)
    assert len(bundle["capabilities"]) > 0


def test_single_file_no_sidecars(populated_conn, tmp_path, monkeypatch):
    summary = _export(populated_conn, "claude:p:s", tmp_path, monkeypatch)
    files = list(summary.bundle_path.parent.glob("*"))
    bundle_files = [f for f in files if f.name == summary.bundle_path.name]
    assert len(bundle_files) == 1
    # No trufflehog.json sidecar should exist next to the bundle.
    assert not (summary.bundle_path.parent / "trufflehog.json").exists()


def test_sha256_reflects_bundle_created_at(populated_conn, tmp_path, monkeypatch):
    """sha256 incorporates bundle_created_at (it's part of the digest input
    minus the manifest), so two exports at different timestamps differ.

    Both exports write to the same default output path (hash-derived from
    session_key), so we compare the in-memory ExportSummary sha256 rather
    than re-reading the file — the second write overwrites the first.
    """
    timestamps = iter(["2026-04-23T17:00:00Z", "2026-04-23T18:00:00Z"])
    monkeypatch.setattr(
        "clawjournal.events.export.bundle._utc_now", lambda: next(timestamps)
    )
    s1 = _export(populated_conn, "claude:p:s", tmp_path, monkeypatch)
    s2 = _export(populated_conn, "claude:p:s", tmp_path, monkeypatch)
    assert s1.sha256 != s2.sha256


def test_global_config_gate_rejects_unconfirmed_source(populated_conn, tmp_path, monkeypatch):
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
    with pytest.raises(ExportGateBlocked, match="Source scope is not confirmed"):
        export_session_bundle(
            populated_conn,
            "claude:p:s",
            config={"source": None, "projects_confirmed": True},
            allow_no_workbench_row=True,
        )


def test_export_rejects_session_with_excluded_secondary_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
    conn = make_conn()
    key = "antigravity:public:mixed"
    insert_event_session(conn, session_key=key, client="antigravity")
    insert_workbench_session(
        conn, session_id="mixed", session_key=key, project="antigravity:public", source="antigravity"
    )
    conn.execute(
        "UPDATE sessions SET project_memberships = ? WHERE session_id = 'mixed'",
        ('["antigravity:public", "antigravity:private"]',),
    )
    config = {**PERMISSIVE_CONFIG, "source": "antigravity", "excluded_projects": ["antigravity:private"]}
    with pytest.raises(ExportGateBlocked, match="belongs to an excluded project"):
        export_session_bundle(conn, key, config=config)


def test_global_config_gate_rejects_unconfirmed_projects(populated_conn, tmp_path, monkeypatch):
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
    with pytest.raises(ExportGateBlocked, match="Project scope is not confirmed"):
        export_session_bundle(
            populated_conn,
            "claude:p:s",
            config={"source": "claude", "projects_confirmed": False},
            allow_no_workbench_row=True,
        )
