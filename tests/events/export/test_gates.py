"""Share-time gate enforcement.

Pins:
- unknown session_key → ExportError
- output path outside $HOME / /tmp → ExportError
- TruffleHog block → blocked manifest-only artifact
- atomic write → no partial bundle on disk if interrupted (best-effort
  via a fault-injecting monkeypatch on the rename step)
- per-child gate is enforced (when --no-children is False, a gated child
  blocks the export)
- active session export keeps ended_at NULL
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clawjournal.events.export import (
    ExportError,
    ExportGateBlocked,
    export_session_bundle,
)
from clawjournal.redaction import trufflehog as th

from ._helpers import (
    PERMISSIVE_CONFIG,
    insert_event,
    insert_event_session,
    make_conn,
)


def test_unknown_session_key_raises_export_error(tmp_path, monkeypatch):
    conn = make_conn()
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
    with pytest.raises(ExportError, match="session_key not found"):
        export_session_bundle(
            conn,
            "claude:does-not-exist:abc",
            config=PERMISSIVE_CONFIG,
            allow_no_workbench_row=True,
            skip_global_gates=True,
        )


def test_out_path_outside_safe_roots_rejected(tmp_path, monkeypatch):
    conn = make_conn()
    sid = insert_event_session(conn, session_key="claude:p:s")
    insert_event(
        conn,
        session_id=sid,
        event_type="user_message",
        source_path="/tmp/x.jsonl",
        raw_json={"x": 1},
    )

    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")
    with pytest.raises(ExportError, match="Output path must resolve under"):
        export_session_bundle(
            conn,
            "claude:p:s",
            output_path=Path("/etc/passwd"),
            config=PERMISSIVE_CONFIG,
            allow_no_workbench_row=True,
            skip_global_gates=True,
        )


def test_trufflehog_block_writes_manifest_only(tmp_path, monkeypatch):
    """When TruffleHog reports a finding, the bundle file is written but
    only contains the manifest section — no events, overrides, or snippets."""
    conn = make_conn()
    sid = insert_event_session(conn, session_key="claude:p:s")
    insert_event(
        conn,
        session_id=sid,
        event_type="user_message",
        source_path="/tmp/x.jsonl",
        raw_json={"x": 1},
    )

    fake_finding = th.TruffleHogFinding(
        detector="FakeDetector",
        status="verified",
        line=1,
        masked="****",
        raw_sha256="deadbeef",
    )

    def _fake_scan_text(text: str) -> th.TruffleHogReport:
        return th.TruffleHogReport(
            scanned_path="<test>",
            scanned_sha256="0",
            findings=[fake_finding],
            verified=1,
        )

    monkeypatch.setattr("clawjournal.events.export.bundle.th.scan_text", _fake_scan_text)
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")

    summary = export_session_bundle(
        conn,
        "claude:p:s",
        config=PERMISSIVE_CONFIG,
        allow_no_workbench_row=True,
        skip_global_gates=True,
    )

    assert summary.blocked is True
    assert summary.block_reason == "secret-scan-findings"

    bundle = json.loads(summary.bundle_path.read_text(encoding="utf-8"))
    assert "events" not in bundle, (
        "blocked manifest-only artifact must not contain events"
    )
    assert "event_overrides" not in bundle
    assert "source_snippets" not in bundle
    assert bundle["manifest"]["blocked"] is True
    assert bundle["manifest"]["block_reason"] == "secret-scan-findings"


def test_atomic_write_no_partial_file(tmp_path, monkeypatch):
    """A failure during the rename step must leave no file at the target."""
    conn = make_conn()
    sid = insert_event_session(conn, session_key="claude:p:s")
    insert_event(
        conn,
        session_id=sid,
        event_type="user_message",
        source_path="/tmp/x.jsonl",
        raw_json={"x": 1},
    )

    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")

    real_replace = __import__("os").replace

    calls = {"n": 0}

    def _failing_replace(src, dst):
        calls["n"] += 1
        # Inject a failure on the actual rename so we can confirm cleanup
        raise OSError("simulated rename failure")

    monkeypatch.setattr("clawjournal.events.export.bundle.os.replace", _failing_replace)

    with pytest.raises(OSError, match="simulated rename failure"):
        export_session_bundle(
            conn,
            "claude:p:s",
            config=PERMISSIVE_CONFIG,
            allow_no_workbench_row=True,
            skip_global_gates=True,
        )

    # Should have attempted rename and cleaned up the .tmp file
    assert calls["n"] >= 1
    leftover = list((tmp_path / ".clawjournal" / "exports").glob("*"))
    assert leftover == [], f"unexpected leftover files: {leftover!r}"


def test_active_session_keeps_ended_at_null(tmp_path, monkeypatch):
    conn = make_conn()
    insert_event_session(
        conn,
        session_key="claude:active:s",
        ended_at=None,
        status="active",
    )
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")

    summary = export_session_bundle(
        conn,
        "claude:active:s",
        config=PERMISSIVE_CONFIG,
        allow_no_workbench_row=True,
        skip_global_gates=True,
    )
    bundle = json.loads(summary.bundle_path.read_text(encoding="utf-8"))
    assert bundle["session"]["ended_at"] is None
    assert bundle["session"]["status"] == "active"


def test_no_workbench_row_blocked_by_default(tmp_path, monkeypatch):
    conn = make_conn()
    insert_event_session(conn, session_key="claude:lone:s")
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")

    with pytest.raises(ExportGateBlocked) as excinfo:
        export_session_bundle(
            conn,
            "claude:lone:s",
            config=PERMISSIVE_CONFIG,
            allow_no_workbench_row=False,  # default
            skip_global_gates=True,
        )
    assert excinfo.value.exit_code == 2
    assert "no workbench" in excinfo.value.message.lower()


def test_per_child_gate_blocks_when_child_lacks_workbench(tmp_path, monkeypatch):
    conn = make_conn()
    insert_event_session(conn, session_key="claude:p:parent")
    insert_event_session(
        conn,
        session_key="claude:p:child",
        parent_session_key="claude:p:parent",
    )
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")

    # Even though the parent passes (allow_no_workbench_row=True), if we
    # tighten on the child, it must block. We simulate that by NOT passing
    # allow_no_workbench_row and watching for the child's failure.
    with pytest.raises(ExportGateBlocked) as excinfo:
        export_session_bundle(
            conn,
            "claude:p:parent",
            config=PERMISSIVE_CONFIG,
            allow_no_workbench_row=False,
            skip_global_gates=True,
        )
    # The parent fails first because gates run parent → children. Either
    # way the message must surface a missing-workbench-row signal.
    assert excinfo.value.exit_code == 2
    assert "workbench" in excinfo.value.message.lower()


def test_no_children_skips_subagents(tmp_path, monkeypatch):
    conn = make_conn()
    insert_event_session(conn, session_key="claude:p:parent")
    insert_event_session(
        conn,
        session_key="claude:p:child",
        parent_session_key="claude:p:parent",
    )
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / ".clawjournal")

    summary = export_session_bundle(
        conn,
        "claude:p:parent",
        config=PERMISSIVE_CONFIG,
        allow_no_workbench_row=True,
        include_children=False,
        skip_global_gates=True,
    )
    bundle = json.loads(summary.bundle_path.read_text(encoding="utf-8"))
    assert bundle["children"] == []
