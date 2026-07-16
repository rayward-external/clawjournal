import json
import hashlib
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from clawjournal import auto_upload
from clawjournal.config import save_config
from clawjournal.parsing import parser
from clawjournal.redaction import trufflehog
from clawjournal.workbench import index


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def auto_db(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr(index, "BLOBS_DIR", tmp_path / "blobs")
    monkeypatch.setattr(
        "clawjournal.workbench.daemon._fetch_hosted_share_capabilities",
        lambda **kwargs: {
            "recurring_upload_api_version": 1,
            "recurring_upload_max_sessions": 5,
            "share_page_url": "https://data.example/share",
            "recurring_consent_version": "consent-v1",
            "recurring_retention_policy_version": "retention-v1",
        },
    )
    conn = index.open_index()
    yield conn
    conn.close()


def _configure(*, source="all"):
    save_config({
        "source": source,
        "excluded_projects": ["codex:excluded"],
        "projects_confirmed": True,
        "verified_email_token": "recurring-token",
        "verified_email_token_expires_at": "2027-01-01T00:00:00+00:00",
    })


def _insert_session(conn, session_id, *, activity, project="codex:project", revision=None):
    conn.execute(
        """INSERT INTO sessions (
            session_id, project, source, indexed_at, updated_at, start_time,
            end_time, review_status, hold_state, content_revision, blob_path
        ) VALUES (?, ?, 'codex', ?, ?, ?, ?, 'new', 'auto_redacted', ?, ?)""",
        (
            session_id, project, activity.isoformat(), activity.isoformat(),
            activity.isoformat(), activity.isoformat(), revision or f"sha256:{session_id}",
            str(index.BLOBS_DIR / f"{session_id}.json"),
        ),
    )
    index.BLOBS_DIR.mkdir(parents=True, exist_ok=True)
    (index.BLOBS_DIR / f"{session_id}.json").write_text("{}", encoding="utf-8")
    conn.commit()


def _enroll(conn, *, source="all", accepted_at=NOW):
    _configure(source=source)
    if conn.execute("SELECT 1 FROM sessions WHERE project = 'codex:project'").fetchone() is None:
        _insert_session(conn, "scope-seed", activity=NOW - timedelta(days=2))
    conn.execute(
        "INSERT INTO shares (share_id, created_at, status, hosted_receipt_id) "
        "VALUES ('manual-share', ?, 'shared', 'manual-receipt')",
        (NOW.isoformat(),),
    )
    conn.commit()
    return auto_upload.enable_enrollment(
        conn,
        consent_version="consent-v1",
        retention_policy_version="retention-v1",
        now=NOW,
        capabilities_fn=lambda: {
            "recurring_upload_api_version": 1,
            "recurring_upload_max_sessions": 5,
            "share_page_url": "https://data.example/share",
        },
        enroll_fn=lambda request: {
            "active_token": "active-token",
            "recovery_token": "recovery-token",
            "enrollment_id": "server-enrollment",
            "authorization_revision": "auth-rev-1",
            "accepted_at": accepted_at.isoformat(),
        },
    )


def test_enrollment_snapshots_scope_and_can_pause(auto_db):
    enrollment = _enroll(auto_db)
    assert enrollment["state"] == "enabled"
    assert enrollment["source_scope"] == "all"
    assert enrollment["excluded_projects"] == ["codex:excluded"]
    assert enrollment["next_due_at"] == (NOW + timedelta(days=7)).isoformat()

    paused = auto_upload.set_enrollment_state(auto_db, "paused", now=NOW)
    assert paused["state"] == "paused"


def test_first_cycle_uses_server_accepted_time(auto_db):
    accepted_at = NOW + timedelta(hours=3)
    enrollment = _enroll(auto_db, accepted_at=accepted_at)
    assert enrollment["server_accepted_at"] == accepted_at.isoformat()
    assert enrollment["next_due_at"] == (accepted_at + timedelta(days=7)).isoformat()


def test_enrollment_requires_manual_receipt_and_recurring_capability(auto_db):
    _configure()
    _insert_session(auto_db, "scope", activity=NOW - timedelta(days=2))
    with pytest.raises(ValueError, match="successful hosted manual share"):
        auto_upload.enable_enrollment(
            auto_db, consent_version="c", retention_policy_version="r",
            capabilities_fn=lambda: {"recurring_upload_api_version": 1,
                                     "recurring_upload_max_sessions": 5},
        )

    auto_db.execute(
        "INSERT INTO shares (share_id, created_at, status, hosted_receipt_id) "
        "VALUES ('manual', ?, 'shared', 'receipt')", (NOW.isoformat(),),
    )
    auto_db.commit()
    with pytest.raises(ValueError, match="capability is unavailable"):
        auto_upload.enable_enrollment(
            auto_db, consent_version="c", retention_policy_version="r",
            capabilities_fn=lambda: {},
        )


def test_selection_uses_enrollment_baseline_and_not_scores(auto_db):
    _insert_session(auto_db, "old", activity=NOW - timedelta(days=1))
    _enroll(auto_db)
    _insert_session(auto_db, "new-unscored", activity=NOW + timedelta(minutes=1))
    _insert_session(
        auto_db, "excluded", activity=NOW + timedelta(minutes=2),
        project="codex:excluded",
    )

    selected = auto_upload.select_pending_sessions(auto_db, now=NOW + timedelta(days=2))
    assert [item["session_id"] for item in selected] == ["new-unscored"]


def test_new_projects_do_not_enter_enrolled_scope(auto_db):
    _enroll(auto_db)
    _insert_session(
        auto_db, "later-project", activity=NOW + timedelta(minutes=1),
        project="codex:new-after-enrollment",
    )
    assert auto_upload.select_pending_sessions(
        auto_db, now=NOW + timedelta(days=2)
    ) == []


def test_selection_is_deterministic_scored_first_and_capped_at_five(auto_db):
    _enroll(auto_db)
    for index_value in range(7):
        session_id = f"candidate-{index_value}"
        _insert_session(
            auto_db, session_id,
            activity=NOW + timedelta(minutes=index_value + 1),
        )
        if index_value < 6:
            auto_db.execute(
                "UPDATE sessions SET ai_failure_value_score = ? WHERE session_id = ?",
                (index_value, session_id),
            )
    auto_db.commit()
    selected = auto_upload.select_pending_sessions(
        auto_db, now=NOW + timedelta(days=2)
    )
    assert [item["session_id"] for item in selected] == [
        "candidate-5", "candidate-4", "candidate-3", "candidate-2", "candidate-1",
    ]


def test_not_due_run_is_quiet(auto_db, monkeypatch):
    _enroll(auto_db)
    monkeypatch.setattr(auto_upload, "open_index", lambda: index.open_index())
    monkeypatch.setattr(auto_upload, "RUN_LOCK", index.INDEX_DB.parent / "run.lock")
    result = auto_upload.run_once(scan=False, now=NOW + timedelta(days=1))
    assert result["status"] == "not_due"
    assert auto_db.execute("SELECT COUNT(*) FROM auto_upload_runs").fetchone()[0] == 0


def test_missed_due_time_catches_up_on_next_wakeup(auto_db, monkeypatch):
    _enroll(auto_db)
    monkeypatch.setattr(auto_upload, "open_index", lambda: index.open_index())
    monkeypatch.setattr(auto_upload, "RUN_LOCK", index.INDEX_DB.parent / "run.lock")
    result = auto_upload.run_once(scan=False, now=NOW + timedelta(days=8))
    assert result == {"ok": True, "status": "no_work", "trace_count": 0}
    run = auto_db.execute(
        "SELECT status, due_at FROM auto_upload_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert run["status"] == "no_work"
    assert run["due_at"] == (NOW + timedelta(days=7)).isoformat()


def test_paused_run_does_not_scan_or_upload(auto_db, monkeypatch):
    _enroll(auto_db)
    auto_upload.set_enrollment_state(auto_db, "paused", now=NOW)
    monkeypatch.setattr(auto_upload, "open_index", lambda: index.open_index())
    monkeypatch.setattr(auto_upload, "RUN_LOCK", index.INDEX_DB.parent / "run.lock")
    result = auto_upload.run_once(
        force=True,
        scanner_factory=lambda: pytest.fail("paused run must not scan"),
        submit_fn=lambda *args, **kwargs: pytest.fail("paused run must not upload"),
    )
    assert result["status"] == "paused"


def test_due_run_uses_real_local_pipeline_and_only_stubs_egress(auto_db, monkeypatch):
    monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
    if not trufflehog.is_available():
        pytest.skip("real local pipeline test requires TruffleHog")
    _insert_session(
        auto_db,
        "scope-real-scan",
        activity=NOW - timedelta(days=2),
        project="codex:real-scan",
        revision=index.compute_content_revision({}),
    )
    _enroll(auto_db, source="codex")

    codex_sessions = index.INDEX_DB.parent / "codex-sessions" / "2026" / "07" / "14"
    codex_sessions.mkdir(parents=True)
    session_file = codex_sessions / "rollout-real-pipeline.jsonl"
    lines = [
        {
            "timestamp": "2026-07-14T12:01:00.000Z",
            "type": "session_meta",
            "payload": {
                "id": "real-pipeline",
                "cwd": "/workspace/real-scan",
                "model_provider": "openai",
            },
        },
        {
            "timestamp": "2026-07-14T12:01:01.000Z",
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-1",
                "cwd": "/workspace/real-scan",
                "model": "gpt-test",
                "effort": "low",
            },
        },
        {
            "timestamp": "2026-07-14T12:01:02.000Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "Inspect the repository and report the result.",
                "images": [],
                "local_images": [],
                "text_elements": [],
            },
        },
        {
            "timestamp": "2026-07-14T12:01:03.000Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "Inspection completed without exposing secrets.",
            },
        },
    ]
    session_file.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(parser, "CODEX_SESSIONS_DIR", index.INDEX_DB.parent / "codex-sessions")
    monkeypatch.setattr(parser, "CODEX_ARCHIVED_DIR", index.INDEX_DB.parent / "codex-archived")
    monkeypatch.setattr(parser, "_CODEX_PROJECT_INDEX", {})
    monkeypatch.setattr(auto_upload, "open_index", lambda: index.open_index())
    monkeypatch.setattr(auto_upload, "RUN_LOCK", index.INDEX_DB.parent / "run.lock")
    monkeypatch.setattr(auto_upload, "CONFIG_DIR", index.INDEX_DB.parent / "config")

    captured = {}

    def fake_server_submit(conn, share_id, **kwargs):
        artifact = Path(kwargs["artifact_path"])
        payload = artifact.read_bytes()
        captured.update({"share_id": share_id, "kwargs": kwargs, "payload": payload})
        assert hashlib.sha256(payload).hexdigest() == conn.execute(
            "SELECT artifact_sha256 FROM auto_upload_runs WHERE share_id = ?",
            (share_id,),
        ).fetchone()[0]
        with zipfile.ZipFile(artifact) as archive:
            assert "manifest.json" in archive.namelist()
        conn.execute(
            "UPDATE shares SET status = 'shared', shared_at = ?, "
            "hosted_receipt_id = 'receipt-real-pipeline' WHERE share_id = ?",
            ((NOW + timedelta(days=7)).isoformat(), share_id),
        )
        conn.commit()
        return {
            "ok": True,
            "receipt_id": "receipt-real-pipeline",
            "session_count": 1,
        }

    result = auto_upload.run_once(
        force=True,
        scan=True,
        now=NOW + timedelta(days=7),
        submit_fn=fake_server_submit,
    )

    assert result["status"] == "succeeded", result
    assert result["trace_count"] == 1
    scanned = auto_db.execute(
        "SELECT project, source, model FROM sessions WHERE session_id = 'real-pipeline'"
    ).fetchone()
    assert dict(scanned) == {
        "project": "codex:real-scan",
        "source": "codex",
        "model": "gpt-test",
    }
    assert captured["kwargs"]["client_submission_id"]
    assert captured["kwargs"]["recurring_enrollment_id"] == "server-enrollment"
    assert captured["kwargs"]["authorization_revision"] == "auth-rev-1"
    assert len(captured["kwargs"]["revision_keys"]) == 1
    run = auto_db.execute(
        "SELECT status, artifact_path, artifact_sha256, client_submission_id, "
        "revisions_json FROM auto_upload_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert run["status"] == "succeeded"
    assert Path(run["artifact_path"]).read_bytes() == captured["payload"]
    assert run["client_submission_id"] == captured["kwargs"]["client_submission_id"]
    assert json.loads(run["revisions_json"]) == captured["kwargs"]["revision_keys"]


def test_failed_upload_reuses_pending_share_then_advances(auto_db, monkeypatch):
    _enroll(auto_db)
    _insert_session(auto_db, "new", activity=NOW + timedelta(minutes=1))
    monkeypatch.setattr(auto_upload, "open_index", lambda: index.open_index())
    monkeypatch.setattr(auto_upload, "RUN_LOCK", index.INDEX_DB.parent / "run.lock")
    artifact = index.INDEX_DB.parent / "artifact.zip"
    artifact.write_bytes(b"exact")
    monkeypatch.setattr(auto_upload, "_seal_artifact", lambda *args: (str(artifact), "hash"))
    package_calls = []
    submit_calls = []

    def fake_package(conn, session_ids, settings, **kwargs):
        package_calls.append(list(session_ids))
        share_id = index.create_share(
            conn,
            session_ids,
            source_filter=settings["source_filter"],
            expected_revisions=kwargs["expected_revisions"],
        )
        return {"ok": True, "share_id": share_id, "export_dir": index.INDEX_DB.parent}

    def fake_submit(conn, share_id, **kwargs):
        submit_calls.append(share_id)
        if len(submit_calls) == 1:
            return {"error": "offline", "status": 502}
        shared_at = (NOW + timedelta(days=7, minutes=1)).isoformat()
        conn.execute(
            "UPDATE shares SET shared_at = ?, status = 'shared', "
            "hosted_receipt_id = 'receipt-1' WHERE share_id = ?",
            (shared_at, share_id),
        )
        conn.commit()
        return {"ok": True, "receipt_id": "receipt-1", "session_count": 1}

    first = auto_upload.run_once(
        force=True, scan=False, now=NOW + timedelta(days=7),
        package_fn=fake_package, submit_fn=fake_submit,
    )
    assert first["status"] == "retrying"
    pending_id = auto_upload.get_enrollment(auto_db)["pending_share_id"]
    assert pending_id == submit_calls[0]

    second = auto_upload.run_once(
        force=True, scan=False, now=NOW + timedelta(days=7, minutes=1),
        package_fn=fake_package, submit_fn=fake_submit,
    )
    assert second == {
        "ok": True,
        "status": "succeeded",
        "share_id": pending_id,
        "trace_count": 1,
        "receipt_id": "receipt-1",
    }
    assert len(package_calls) == 1
    assert submit_calls == [pending_id, pending_id]
    enrollment = auto_upload.get_enrollment(auto_db)
    assert enrollment["pending_share_id"] is None
    assert enrollment["last_receipt_id"] == "receipt-1"
    assert auto_upload.select_pending_sessions(auto_db) == []


def test_ambiguous_upload_requires_action_without_dropping_pending(auto_db, monkeypatch):
    _enroll(auto_db)
    _insert_session(auto_db, "new", activity=NOW + timedelta(minutes=1))
    monkeypatch.setattr(auto_upload, "open_index", lambda: index.open_index())
    monkeypatch.setattr(auto_upload, "RUN_LOCK", index.INDEX_DB.parent / "run.lock")
    artifact = index.INDEX_DB.parent / "artifact.zip"
    artifact.write_bytes(b"exact")
    monkeypatch.setattr(auto_upload, "_seal_artifact", lambda *args: (str(artifact), "hash"))
    monkeypatch.setattr(auto_upload, "_lookup_receipt", lambda *args: {})

    def fake_package(conn, session_ids, settings, **kwargs):
        share_id = index.create_share(
            conn, session_ids, expected_revisions=kwargs["expected_revisions"]
        )
        return {"ok": True, "share_id": share_id, "export_dir": index.INDEX_DB.parent}

    result = auto_upload.run_once(
        force=True,
        scan=False,
        now=NOW + timedelta(days=7),
        package_fn=fake_package,
        submit_fn=lambda *args, **kwargs: {
            "error": "confirmation timed out", "status": 504, "ambiguous": True,
        },
    )
    assert result["status"] == "action_required"
    assert result["required_action"] == "reconcile_submission_receipt"
    assert auto_upload.get_enrollment(auto_db)["pending_share_id"] == result["share_id"]
