"""Adversarial tests for the automatic weekly-upload state machine.

These tests deliberately inject failures at durable-state and egress
boundaries.  They use only isolated config/index roots and never touch real
agent hook files or the network.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from clawjournal import agent_hooks, auto_upload as auto
from clawjournal import config as config_module
from clawjournal.auto_upload_client import RecurringServiceError
from clawjournal.auto_upload_credentials import (
    CredentialStoreError,
    credential_path,
    load_credentials,
    write_credentials,
)
from clawjournal.findings import allowlist_add_by_hash, allowlist_remove
from clawjournal.workbench import index as index_module
from clawjournal.workbench.index import (
    add_policy,
    create_share,
    get_auto_upload_enrollment,
    open_index,
    remove_policy,
    save_auto_upload_enrollment,
    set_hold_state,
    update_auto_upload_enrollment,
    upsert_sessions,
)


ORIGIN = "https://data.rayward.ai"
ENROLLED_AT = "2026-07-10T12:00:00+00:00"
AUTH_VERSION = "recurring-v1"
RETENTION_VERSION = "retention-v1"


@pytest.fixture
def isolated_auto_upload(tmp_path, monkeypatch):
    install_dir = tmp_path / ".clawjournal"
    config_file = install_dir / "config.json"
    index_path = install_dir / "index.db"
    monkeypatch.setattr(config_module, "CONFIG_DIR", install_dir)
    monkeypatch.setattr(config_module, "CONFIG_FILE", config_file)
    monkeypatch.setattr(index_module, "CONFIG_DIR", install_dir)
    monkeypatch.setattr(index_module, "INDEX_DB", index_path)
    monkeypatch.setattr(index_module, "BLOBS_DIR", install_dir / "blobs")
    monkeypatch.setattr(
        auto,
        "hook_diagnostics",
        lambda target, *, last_observed_at: {
            "target": target,
            "configured": False,
            "observed": bool(last_observed_at),
        },
    )
    return {
        "root": tmp_path,
        "install": install_dir,
        "config": config_file,
        "index": index_path,
    }


def _save_scope_config(*, upload_token: str | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {
        **config_module.DEFAULT_CONFIG,
        "source": "claude",
        "projects_confirmed": True,
        "ai_pii_review_enabled": False,
    }
    if upload_token is not None:
        config["verified_email_token"] = upload_token
        config["verified_email_token_expires_at"] = "2099-01-01T00:00:00+00:00"
    config_module.save_config(config)
    return config


def _session(
    root: Path,
    session_id: str,
    *,
    project: str = "project-one",
) -> tuple[dict[str, Any], Path]:
    raw_path = root / "raw" / f"{session_id}.jsonl"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(f'{{"session":"{session_id}"}}\n', encoding="utf-8")
    return (
        {
            "session_id": session_id,
            "project": project,
            "source": "claude",
            "model": "test-model",
            "start_time": "2026-07-12T08:00:00+00:00",
            "end_time": "2026-07-12T09:00:00+00:00",
            "raw_source_path": str(raw_path),
            "messages": [
                {"role": "user", "content": f"work on {session_id}", "tool_uses": []},
                {"role": "assistant", "content": "done", "tool_uses": []},
            ],
            "stats": {
                "user_messages": 1,
                "assistant_messages": 1,
                "tool_uses": 0,
                "input_tokens": 5,
                "output_tokens": 2,
            },
        },
        raw_path,
    )


def _seed_released_session(
    conn,
    root: Path,
    session_id: str = "session-one",
) -> Path:
    session, raw_path = _session(root, session_id)
    assert upsert_sessions(conn, [session]) == 1
    conn.execute(
        "UPDATE sessions SET revision_stable_since = ? WHERE session_id = ?",
        ("2026-07-12T09:00:00+00:00", session_id),
    )
    conn.commit()
    assert set_hold_state(
        conn,
        session_id,
        "released",
        changed_by="test",
        reason="fixture",
    )
    return raw_path


def _credentials(enrollment_id: str = "server-enrollment-1") -> dict[str, str]:
    return {
        "issuer": ORIGIN,
        "api_origin": ORIGIN,
        "enrollment_id": enrollment_id,
        "active_token": "active-secret",
        "active_token_expires_at": "2099-01-01T00:00:00+00:00",
        "recovery_token": "recovery-secret",
        "recovery_token_expires_at": "2099-02-01T00:00:00+00:00",
    }


def _capabilities(origin: str = ORIGIN) -> dict[str, Any]:
    return {
        "origin": origin,
        "maximum_bundle_size": 5_000_000,
        "recurring_enrollment_url": f"{origin}/api/recurring-enrollments",
        "recurring_submission_url": f"{origin}/api/recurring-submissions",
        "recurring_receipt_lookup_url": (
            f"{origin}/api/recurring-receipts/{{client_submission_id}}"
        ),
    }


def _terms() -> dict[str, str]:
    return {
        "authorization_version": AUTH_VERSION,
        "authorization_text": "I authorize a weekly upload of up to five traces.",
        "retention_policy_version": RETENTION_VERSION,
        "retention_text": "Uploaded traces follow the stated retention policy.",
    }


def _save_enabled_enrollment(
    conn,
    config: dict[str, Any],
    *,
    enrollment_id: str = "server-enrollment-1",
    generation: int = 1,
    current_run_id: str | None = None,
    enrolled_at: str = ENROLLED_AT,
    health: str = "ready",
    enrolled_projects: list[str] | None = None,
) -> dict[str, Any]:
    projects = enrolled_projects or ["project-one"]
    profile = auto.egress_profile_hash(
        conn,
        enrollment_scope={"sources": ["claude"], "projects": projects},
        api_origin=ORIGIN,
        ai_backend=None,
        config=config,
    )
    return save_auto_upload_enrollment(
        conn,
        mode="enabled",
        health=health,
        generation=generation,
        enrolled_at=enrolled_at,
        client_enrollment_id=f"client-{enrollment_id}",
        enrolled_sources=["claude"],
        enrolled_projects=projects,
        server_enrollment_id=enrollment_id,
        authorization_revision=1,
        recurring_authorization_version=AUTH_VERSION,
        retention_version=RETENTION_VERSION,
        egress_profile_hash=profile,
        hook_targets=["claude", "codex"],
        current_run_id=current_run_id,
        current_run_stage="packaging" if current_run_id else None,
    )


def _patch_strict_scanner(
    monkeypatch,
    results: list[dict[str, Any]] | None = None,
    callbacks: list[Callable[[], None] | None] | None = None,
):
    responses = list(results or [{"ok": True, "sources": ["claude"]}])
    actions = list(callbacks or [])
    calls: list[list[str]] = []

    class StrictScanner:
        def scan_once_strict(self, required_sources):
            calls.append(list(required_sources))
            index = len(calls) - 1
            if index < len(actions) and actions[index] is not None:
                actions[index]()
            return responses[min(index, len(responses) - 1)]

    monkeypatch.setattr("clawjournal.workbench.daemon.Scanner", StrictScanner)
    return calls


def _create_pending_share(
    conn,
    install_dir: Path,
    *,
    session_id: str,
    enrollment_id: str,
    state: str,
    client_submission_id: str | None = None,
) -> tuple[str, Path]:
    share_id = create_share(conn, [session_id])
    export_dir = install_dir / "shares" / share_id
    export_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = export_dir / auto.SEALED_ZIP_FILENAME
    artifact_path.write_bytes(b"exact-sealed-zip-bytes")
    raw_path = conn.execute(
        "SELECT raw_source_path FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()[0]
    fingerprints = auto._raw_fingerprints(
        [{"session_id": session_id, "raw_source_path": raw_path}]
    )
    conn.execute(
        "UPDATE shares SET submission_channel = 'auto_weekly', enrollment_id = ?, "
        "client_submission_id = ?, authorization_revision = 1, submission_state = ?, "
        "sealed_artifact_sha256 = ?, sealed_artifact_path = ?, "
        "sealed_raw_fingerprints = ? WHERE share_id = ?",
        (
            enrollment_id,
            client_submission_id or f"submission-{share_id}",
            state,
            hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            str(artifact_path),
            json.dumps(
                {key: list(value) for key, value in fingerprints.items()},
                sort_keys=True,
                separators=(",", ":"),
            ),
            share_id,
        ),
    )
    conn.commit()
    return share_id, artifact_path


def _seed_changed_approved_revision(conn, root: Path) -> str:
    """Seed a revision whose automatic eligibility depends on fresh approval."""
    _seed_released_session(conn, root)
    prior_share_id = create_share(conn, ["session-one"])
    conn.execute(
        "UPDATE shares SET status = 'shared', shared_at = ? WHERE share_id = ?",
        ("2026-07-13T00:00:00+00:00", prior_share_id),
    )
    revision = "changed-revision-needing-fresh-approval"
    conn.execute(
        "UPDATE sessions SET content_revision = ?, review_status = 'approved' "
        "WHERE session_id = 'session-one'",
        (revision,),
    )
    conn.commit()
    return revision


def _patch_runner_host(monkeypatch, *, origin: str = ORIGIN) -> None:
    monkeypatch.setattr(auto, "fetch_capabilities", lambda **_kwargs: _capabilities(origin))
    monkeypatch.setattr(
        auto,
        "get_enrollment",
        lambda *_args, **_kwargs: {
            "enrollment_id": "server-enrollment-1",
            "submissions_open": True,
            "terms_current": True,
            "authorization_revision": 1,
            "authorization_version": AUTH_VERSION,
            "retention_policy_version": RETENTION_VERSION,
            "scope_hash": auto.scope_hash(["claude"], ["project-one"]),
            "revoked_at": None,
        },
    )


def test_status_is_read_only_without_an_install(
    isolated_auto_upload,
    monkeypatch,
):
    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("status must not call the network")

    for name in (
        "fetch_capabilities",
        "fetch_authorization",
        "get_enrollment",
        "lookup_receipt",
        "submit_artifact",
    ):
        monkeypatch.setattr(auto, name, network_forbidden)

    result = auto.status()

    assert result["mode"] == "off"
    assert result["offer_available"] is False
    assert not isolated_auto_upload["index"].exists()
    assert not isolated_auto_upload["config"].exists()
    assert not isolated_auto_upload["install"].exists()


def test_auto_upload_ui_is_hidden_unless_internal_rollout_is_enabled(
    isolated_auto_upload,
    monkeypatch,
):
    monkeypatch.delenv(auto.AUTO_UPLOAD_UI_ENV, raising=False)
    assert auto.status()["ui_visible"] is False

    monkeypatch.setenv(auto.AUTO_UPLOAD_UI_ENV, "1")
    assert auto.status()["ui_visible"] is True


def test_existing_auto_upload_authority_keeps_controls_visible(
    isolated_auto_upload,
    monkeypatch,
):
    monkeypatch.delenv(auto.AUTO_UPLOAD_UI_ENV, raising=False)
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config)
    conn.close()

    assert auto.status()["ui_visible"] is True


@pytest.mark.parametrize("receipt_kind", ["missing", "auto_weekly"])
def test_enable_requires_a_successful_hosted_manual_receipt_before_network(
    isolated_auto_upload,
    monkeypatch,
    receipt_kind,
):
    _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    if receipt_kind == "auto_weekly":
        share_id = create_share(conn, ["session-one"])
        conn.execute(
            "UPDATE shares SET status = 'shared', shared_at = ?, "
            "hosted_receipt_id = ?, submission_channel = 'auto_weekly' "
            "WHERE share_id = ?",
            (
                "2026-07-13T00:00:00+00:00",
                "automatic-receipt-does-not-qualify",
                share_id,
            ),
        )
        conn.commit()
    conn.close()

    forbidden_calls: list[str] = []

    class ScannerForbidden:
        def scan_once_strict(self, _required_sources):
            forbidden_calls.append("scan")
            raise AssertionError("manual-receipt gate must run before strict scan")

    def network_forbidden(*_args, **_kwargs):
        forbidden_calls.append("network")
        raise AssertionError("manual-receipt gate must run before network")

    monkeypatch.setattr("clawjournal.workbench.daemon.Scanner", ScannerForbidden)
    monkeypatch.setattr(auto, "fetch_capabilities", network_forbidden)

    result = auto.enable(agent="claude")

    assert result["ok"] is False
    assert result["code"] == "manual_share_required"
    assert forbidden_calls == []
    assert not credential_path().exists()


@pytest.mark.parametrize("submission_channel", [None, "manual"])
def test_enable_accepts_legacy_and_explicit_manual_receipts(
    isolated_auto_upload,
    monkeypatch,
    submission_channel,
):
    _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    share_id = create_share(conn, ["session-one"])
    conn.execute(
        "UPDATE shares SET status = 'shared', shared_at = ?, "
        "hosted_receipt_id = ?, submission_channel = ? WHERE share_id = ?",
        (
            "2026-07-13T00:00:00+00:00",
            "successful-manual-receipt",
            submission_channel,
            share_id,
        ),
    )
    conn.commit()
    conn.close()
    _patch_strict_scanner(monkeypatch)
    monkeypatch.setattr(auto, "fetch_capabilities", lambda **_kwargs: _capabilities())
    monkeypatch.setattr(auto, "fetch_authorization", lambda _caps: _terms())

    result = auto.enable(agent="claude", challenge_only=True)

    assert result["code"] == "authorization_required"


def test_v1_scope_rejects_sources_without_audited_raw_snapshot(
    isolated_auto_upload,
):
    conn = open_index()
    session, _raw_path = _session(isolated_auto_upload["root"], "other-source")
    session["source"] = "workbuddy"
    session["project"] = "workbuddy:project"
    upsert_sessions(conn, [session])
    config = {
        **config_module.DEFAULT_CONFIG,
        "source": "workbuddy",
        "projects_confirmed": True,
    }

    scope = auto._current_scope(conn, config)

    assert "unsupported_source" in scope["blockers"]
    assert scope["unsupported_sources"] == ["workbuddy"]
    conn.close()


def test_hook_adapters_are_inert_without_an_existing_index(isolated_auto_upload):
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    assert auto.record_hook_observed("claude", now) is False
    assert auto.hook_due_check("claude", now) == agent_hooks.DueDecision(
        False, "index-unavailable"
    )
    assert auto.hook_session_start_check(
        "claude", now
    ) == agent_hooks.DueDecision(False, "index-unavailable")

    assert not isolated_auto_upload["index"].exists()
    assert not isolated_auto_upload["config"].exists()
    assert not isolated_auto_upload["install"].exists()


def test_writer_lock_makes_real_hook_path_return_quickly_without_runner(
    isolated_auto_upload,
):
    config = _save_scope_config()
    conn = open_index()
    _save_enabled_enrollment(
        conn,
        config,
        enrolled_at="2026-07-01T00:00:00+00:00",
    )
    conn.close()

    locker = sqlite3.connect(isolated_auto_upload["index"], timeout=0.1)
    locker.execute("BEGIN IMMEDIATE")
    spawned: list[str] = []
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    started = time.monotonic()
    try:
        assert auto.record_hook_observed("claude", now) is False
        assert auto.hook_due_check("claude", now) == agent_hooks.DueDecision(
            False, "index-busy"
        )
        result = agent_hooks.run_session_start(
            client="claude",
            now=now,
            due_check=auto.hook_session_start_check,
            spawn_runner=lambda client: spawned.append(client) or True,
        )
    finally:
        locker.rollback()
        locker.close()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert result == agent_hooks.SessionStartResult(
        observed_at=now,
        reason="index-busy",
    )
    assert spawned == []


def test_missing_selected_hook_is_action_required_but_run_now_stays_allowed(
    isolated_auto_upload,
):
    config = _save_scope_config()
    conn = open_index()
    _save_enabled_enrollment(conn, config)

    result = auto.status(conn=conn)

    assert result["health"] == "action_required"
    assert result["run_now_allowed"] is True
    assert get_auto_upload_enrollment(conn)["health"] == "ready"
    conn.close()


def test_preview_without_refresh_is_read_only_without_an_install(
    isolated_auto_upload,
    monkeypatch,
):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("read-only preview must not scan, migrate, or use network")

    for name in (
        "fetch_capabilities",
        "fetch_authorization",
        "get_enrollment",
        "lookup_receipt",
        "submit_artifact",
    ):
        monkeypatch.setattr(auto, name, forbidden)
    monkeypatch.setattr(index_module, "open_index", forbidden)

    result = auto.preview(refresh=False)

    assert result["ok"] is True
    assert result["selected"] == []
    assert result["scope_blockers"] == ["not_enrolled"]
    assert not isolated_auto_upload["index"].exists()
    assert not isolated_auto_upload["config"].exists()
    assert not isolated_auto_upload["install"].exists()


def test_stale_run_overlay_never_owns_due_or_recovery_lock():
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    enrollment = {
        "mode": "enabled",
        "enrolled_at": (now - timedelta(days=8)).isoformat(),
        "last_completed_at": None,
        "next_retry_at": None,
        "current_run_id": "crashed-process-overlay",
        "current_run_stage": "submitting",
    }

    decision = auto.due_decision(enrollment, now=now)

    assert decision.due is True
    assert decision.reason == "due"


def test_run_clears_stale_overlay_and_origin_drift_sends_no_bearer(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(
        conn,
        config,
        current_run_id="dead-run",
        enrolled_at="2026-07-01T00:00:00+00:00",
    )
    conn.close()
    monkeypatch.setattr(auto, "load_credentials", lambda **_kwargs: _credentials())
    monkeypatch.setattr(
        auto,
        "fetch_capabilities",
        lambda **_kwargs: _capabilities("https://different.example"),
    )
    _patch_strict_scanner(monkeypatch)
    authenticated_calls: list[str] = []

    def bearer_forbidden(*_args, **_kwargs):
        authenticated_calls.append("called")
        raise AssertionError("origin drift must stop before bearer use")

    monkeypatch.setattr(auto, "get_enrollment", bearer_forbidden)
    monkeypatch.setattr(auto, "lookup_receipt", bearer_forbidden)
    monkeypatch.setattr(auto, "submit_artifact", bearer_forbidden)

    result = auto.run_cycle(force=True)

    assert result["code"] == "destination_changed"
    assert authenticated_calls == []
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["current_run_id"] is None
        assert enrollment["current_run_stage"] is None
    finally:
        conn.close()


def test_runner_crash_records_backoff_before_returning(isolated_auto_upload, monkeypatch):
    """A hard crash must stamp next_retry_at so hooks don't relaunch instantly."""
    config = _save_scope_config()
    conn = open_index()
    _save_enabled_enrollment(conn, config)
    conn.close()

    def boom(**_kwargs):
        raise RuntimeError("unexpected runner crash")

    monkeypatch.setattr(auto, "_run_cycle_impl", boom)

    result = auto.run_cycle(force=True)

    assert result["ok"] is False
    assert result["code"] == "runner_crash"
    assert result["retryable"] is True
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["health"] == "retrying"
        assert enrollment["consecutive_failures"] == 1
        assert enrollment["next_retry_at"] is not None
        decision = auto.due_decision(
            enrollment, now=datetime.now(timezone.utc)
        )
        assert decision.due is False
    finally:
        conn.close()


def test_disable_removes_upload_authority_before_failed_hook_cleanup_and_reconciles(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"], "sealed-session")
    _seed_released_session(conn, isolated_auto_upload["root"], "submitting-session")
    _save_enabled_enrollment(conn, config)
    sealed_id, sealed_path = _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="sealed-session",
        enrollment_id="server-enrollment-1",
        state="sealed",
    )
    submitting_id, _ = _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="submitting-session",
        enrollment_id="server-enrollment-1",
        state="submitting",
    )
    conn.close()
    write_credentials(_credentials())
    ordering_checks: list[str] = []

    def assert_recovery_only(label: str) -> None:
        record = load_credentials(required=True)
        assert record["active_token"] is None
        assert record["active_token_expires_at"] is None
        assert record["recovery_token"] == "recovery-secret"
        ordering_checks.append(label)

    def uninstall_fails(_target):
        assert_recovery_only("hook")
        raise OSError("agent config became read-only")

    def recovery_caps(origin):
        assert origin == ORIGIN
        assert_recovery_only("capabilities")
        return _capabilities(origin)

    def receipt(*_args, **_kwargs):
        assert_recovery_only("receipt")
        return {
            "receipt_id": "receipt-after-crash",
            "accepted_at": "2026-07-15T10:00:00+00:00",
            "status": "accepted",
        }

    def revoke(*_args, **kwargs):
        assert kwargs["recovery_token"] == "recovery-secret"
        assert_recovery_only("revoke")
        return {"ok": True}

    monkeypatch.setattr(auto, "uninstall_agent_hook", uninstall_fails)
    monkeypatch.setattr(auto, "recovery_capabilities", recovery_caps)
    monkeypatch.setattr(auto, "lookup_receipt", receipt)
    monkeypatch.setattr(auto, "revoke_enrollment", revoke)

    result = auto.disable()

    assert result["mode"] == "off"
    assert result["health"] == "action_required"
    assert result["last_result"]["code"] == "hook_cleanup_failed"
    assert {"hook", "capabilities", "receipt", "revoke"} <= set(ordering_checks)
    assert ordering_checks.index("revoke") < ordering_checks.index("receipt")
    assert not credential_path().exists()
    assert not sealed_path.exists()
    conn = open_index()
    try:
        assert conn.execute(
            "SELECT 1 FROM shares WHERE share_id = ?", (sealed_id,)
        ).fetchone() is None
        submitted = conn.execute(
            "SELECT submission_state, hosted_receipt_id FROM shares WHERE share_id = ?",
            (submitting_id,),
        ).fetchone()
        assert tuple(submitted) == ("accepted", "receipt-after-crash")
    finally:
        conn.close()


def test_stale_receipt_probe_cannot_strand_sealed_draft_after_disable_wins(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    stale_enrollment = _save_enabled_enrollment(conn, config)
    share_id, _ = _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="submitting",
    )
    assert update_auto_upload_enrollment(
        conn,
        expected_generation=1,
        mode="off",
        generation=2,
        revocation_pending=True,
        last_result_code="disabling",
    )
    monkeypatch.setattr(
        auto,
        "lookup_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RecurringServiceError("receipt_not_found", "not found", status=404)
        ),
    )

    stale_result = auto._reconcile_pending(
        conn,
        enrollment=stale_enrollment,
        credentials=_credentials(),
        capabilities=_capabilities(),
        allow_submit=False,
    )

    assert stale_result["code"] == "disabled"
    assert conn.execute(
        "SELECT submission_state FROM shares WHERE share_id = ?", (share_id,)
    ).fetchone()[0] == "submitting"
    conn.close()

    write_credentials(_credentials())
    monkeypatch.setattr(auto, "uninstall_agent_hook", lambda _target: None)
    monkeypatch.setattr(
        auto, "recovery_capabilities", lambda origin: _capabilities(origin)
    )
    monkeypatch.setattr(auto, "revoke_enrollment", lambda *_args, **_kwargs: {"ok": True})

    disabled = auto.disable()

    assert disabled["mode"] == "off"
    assert disabled["overlay"] is None
    assert not credential_path().exists()
    conn = open_index()
    try:
        assert conn.execute(
            "SELECT submission_state FROM shares WHERE share_id = ?", (share_id,)
        ).fetchone()[0] == "not_found"
    finally:
        conn.close()


def test_disable_is_idempotent_after_definite_revocation(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config)
    conn.close()
    write_credentials(_credentials())
    monkeypatch.setattr(auto, "uninstall_agent_hook", lambda _target: None)
    monkeypatch.setattr(
        auto, "recovery_capabilities", lambda origin: _capabilities(origin)
    )
    revoke_calls: list[str] = []
    monkeypatch.setattr(
        auto,
        "revoke_enrollment",
        lambda _caps, *, enrollment_id, recovery_token: revoke_calls.append(
            enrollment_id
        ),
    )

    first = auto.disable()
    second = auto.disable()

    assert first["mode"] == second["mode"] == "off"
    assert first["health"] == second["health"] == "ready"
    assert first["overlay"] is second["overlay"] is None
    assert second["last_result"]["code"] == "disabled"
    assert revoke_calls == ["server-enrollment-1"]
    assert not credential_path().exists()


def test_disable_retry_finishes_revocation_with_recovery_tombstone(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config)
    conn.close()
    write_credentials(_credentials())
    monkeypatch.setattr(auto, "uninstall_agent_hook", lambda _target: None)
    monkeypatch.setattr(
        auto, "recovery_capabilities", lambda origin: _capabilities(origin)
    )
    attempts = 0

    def flaky_revoke(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RecurringServiceError(
                "network_error", "temporary outage", retryable=True
            )
        return {"ok": True}

    monkeypatch.setattr(auto, "revoke_enrollment", flaky_revoke)

    pending = auto.disable()
    assert pending["mode"] == "off"
    assert pending["health"] == "retrying"
    assert pending["overlay"] == "revocation_pending"
    tombstone = load_credentials(required=True)
    assert tombstone["active_token"] is None
    assert tombstone["recovery_token"] == "recovery-secret"

    recovered = auto.disable()

    assert recovered["mode"] == "off"
    assert recovered["health"] == "ready"
    assert recovered["overlay"] is None
    assert recovered["last_result"]["code"] == "disabled"
    assert attempts == 2
    assert not credential_path().exists()


def test_prior_enrollment_pending_artifact_cannot_cross_reenrollment(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config, enrollment_id="new-enrollment")
    _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="old-enrollment",
        state="sealed",
    )
    conn.close()
    monkeypatch.setattr(
        auto,
        "load_credentials",
        lambda **_kwargs: _credentials("new-enrollment"),
    )

    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("old-enrollment artifact must stop before network")

    monkeypatch.setattr(auto, "fetch_capabilities", network_forbidden)
    monkeypatch.setattr(auto, "lookup_receipt", network_forbidden)
    monkeypatch.setattr(auto, "submit_artifact", network_forbidden)

    result = auto.run_cycle(force=True)

    assert result["code"] == "receipt_reconciliation_pending"


@pytest.mark.parametrize("mutation", ["hold", "review", "revision", "raw"])
def test_recovery_rechecks_hold_review_revision_and_raw_fingerprint(
    isolated_auto_upload,
    mutation,
):
    config = _save_scope_config()
    conn = open_index()
    raw_path = _seed_released_session(conn, isolated_auto_upload["root"])
    enrollment = _save_enabled_enrollment(conn, config)
    share_id, _ = _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="sealed",
    )
    share = dict(
        conn.execute("SELECT * FROM shares WHERE share_id = ?", (share_id,)).fetchone()
    )

    # Establish that the fixture itself clears every recovery gate.
    auto._validate_pending_for_submission(
        conn,
        share=share,
        enrollment=enrollment,
        expected_profile_hash=enrollment["egress_profile_hash"],
        api_origin=ORIGIN,
        ai_backend=None,
    )

    if mutation == "hold":
        set_hold_state(
            conn,
            "session-one",
            "pending_review",
            changed_by="test",
            reason="late privacy hold",
        )
    elif mutation == "review":
        conn.execute(
            "UPDATE sessions SET review_status = 'blocked' WHERE session_id = ?",
            ("session-one",),
        )
        conn.commit()
    elif mutation == "revision":
        conn.execute(
            "UPDATE sessions SET content_revision = ? WHERE session_id = ?",
            ("changed-after-seal", "session-one"),
        )
        conn.commit()
    else:
        raw_path.write_text("changed after sealing\n", encoding="utf-8")

    with pytest.raises(auto.ControlChanged):
        auto._validate_pending_for_submission(
            conn,
            share=share,
            enrollment=enrollment,
            expected_profile_hash=enrollment["egress_profile_hash"],
            api_origin=ORIGIN,
            ai_backend=None,
        )
    conn.close()


def test_validate_pending_skips_raw_hash_when_disabled(isolated_auto_upload):
    """check_raw_fingerprints=False keeps the size-unbounded raw re-hash out of
    the egress lock: the locked submit path validates the ledger just before the
    lock, so passing False here must not re-hash (a raw change alone won't raise),
    while the default still re-hashes and catches the change."""
    config = _save_scope_config()
    conn = open_index()
    raw_path = _seed_released_session(conn, isolated_auto_upload["root"])
    enrollment = _save_enabled_enrollment(conn, config)
    share_id, _ = _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="sealed",
    )
    share = dict(
        conn.execute("SELECT * FROM shares WHERE share_id = ?", (share_id,)).fetchone()
    )
    raw_path.write_text("changed after sealing\n", encoding="utf-8")

    # Guard against a regression that moves the raw hash back under the lock: with
    # the check disabled the raw mutation is not re-hashed here, so nothing raises.
    called = False
    original = auto._validate_raw_fingerprint_ledger

    def _spy(*args, **kwargs):
        nonlocal called
        called = True
        return original(*args, **kwargs)

    auto._validate_raw_fingerprint_ledger = _spy
    try:
        auto._validate_pending_for_submission(
            conn,
            share=share,
            enrollment=enrollment,
            expected_profile_hash=enrollment["egress_profile_hash"],
            api_origin=ORIGIN,
            ai_backend=None,
            check_raw_fingerprints=False,
        )
        assert called is False

        # The default still re-hashes and catches the post-seal raw change.
        with pytest.raises(auto.ControlChanged):
            auto._validate_pending_for_submission(
                conn,
                share=share,
                enrollment=enrollment,
                expected_profile_hash=enrollment["egress_profile_hash"],
                api_origin=ORIGIN,
                ai_backend=None,
            )
        assert called is True
    finally:
        auto._validate_raw_fingerprint_ledger = original
    conn.close()


@pytest.mark.parametrize("review_status", ["new", "blocked"])
def test_revoked_fresh_approval_stops_before_ai_and_submit(
    isolated_auto_upload,
    monkeypatch,
    review_status,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_changed_approved_revision(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config)
    conn.close()
    write_credentials(_credentials())
    _patch_runner_host(monkeypatch)
    _patch_strict_scanner(monkeypatch)
    external_calls: list[str] = []

    def revoke_in_package(conn, _session_ids, _settings, **kwargs):
        conn.execute(
            "UPDATE sessions SET review_status = ? WHERE session_id = 'session-one'",
            (review_status,),
        )
        conn.commit()
        kwargs["before_ai_call"]()
        external_calls.append("ai")
        raise AssertionError("revoked approval must stop before AI")

    monkeypatch.setattr(auto, "package", revoke_in_package)
    monkeypatch.setattr(
        auto,
        "submit_artifact",
        lambda *_args, **_kwargs: external_calls.append("submit"),
    )

    result = auto.run_cycle(force=True)

    assert result["code"] == "control_changed"
    assert external_calls == []


@pytest.mark.parametrize("boundary", ["seal", "submit"])
def test_revoked_fresh_approval_stops_atomic_artifact_boundaries(
    isolated_auto_upload,
    boundary,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_changed_approved_revision(conn, isolated_auto_upload["root"])
    enrollment = _save_enabled_enrollment(conn, config)

    if boundary == "seal":
        share_id = create_share(conn, ["session-one"])
        artifact_dir = isolated_auto_upload["install"] / "shares" / share_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / auto.SEALED_ZIP_FILENAME
        artifact_path.write_bytes(b"not-yet-sealed")
        raw_path = conn.execute(
            "SELECT raw_source_path FROM sessions WHERE session_id = 'session-one'"
        ).fetchone()[0]
        fingerprints = auto._raw_fingerprints([
            {"session_id": "session-one", "raw_source_path": raw_path}
        ])
    else:
        share_id, _ = _create_pending_share(
            conn,
            isolated_auto_upload["install"],
            session_id="session-one",
            enrollment_id="server-enrollment-1",
            state="sealed",
        )

    conn.execute(
        "UPDATE sessions SET review_status = 'new' WHERE session_id = 'session-one'"
    )
    conn.commit()

    if boundary == "seal":
        with pytest.raises(auto.ControlChanged):
            auto._seal_share_ledger(
                conn,
                share_id=share_id,
                enrollment=enrollment,
                artifact_path=artifact_path,
                artifact_sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                raw_fingerprints=fingerprints,
            )
        expected_state = None
    else:
        assert auto._transition_submission(
            conn,
            share_id=share_id,
            from_state="sealed",
            to_state="submitting",
            generation=int(enrollment["generation"]),
        ) is False
        expected_state = "sealed"

    state = conn.execute(
        "SELECT submission_state FROM shares WHERE share_id = ?", (share_id,)
    ).fetchone()[0]
    assert state == expected_state
    conn.close()


@pytest.mark.parametrize("mutation", ["revision", "raw"])
def test_stale_sealed_recovery_is_discarded_so_later_revision_can_progress(
    isolated_auto_upload,
    monkeypatch,
    mutation,
):
    config = _save_scope_config()
    conn = open_index()
    raw_path = _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config)
    share_id, artifact_path = _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="sealed",
    )
    if mutation == "revision":
        conn.execute(
            "UPDATE sessions SET content_revision = ? WHERE session_id = ?",
            ("changed-after-seal", "session-one"),
        )
        conn.commit()
    else:
        raw_path.write_text("changed after sealing\n", encoding="utf-8")
    conn.close()
    write_credentials(_credentials())
    _patch_runner_host(monkeypatch)
    _patch_strict_scanner(monkeypatch)
    post_calls: list[str] = []
    monkeypatch.setattr(
        auto,
        "submit_artifact",
        lambda *_args, **_kwargs: post_calls.append("POST"),
    )

    result = auto.run_cycle(force=True)

    assert result["code"] == "control_changed"
    assert post_calls == []
    assert not artifact_path.exists()
    conn = open_index()
    try:
        assert conn.execute(
            "SELECT 1 FROM shares WHERE share_id = ?", (share_id,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT share_id FROM sessions WHERE session_id = ?", ("session-one",)
        ).fetchone()[0] is None
    finally:
        conn.close()


def test_pending_recovery_requires_a_fresh_strict_scan(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config)
    _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="sealed",
    )
    conn.close()
    monkeypatch.setattr(auto, "load_credentials", lambda **_kwargs: _credentials())
    _patch_runner_host(monkeypatch)
    calls = _patch_strict_scanner(
        monkeypatch,
        results=[{"ok": False, "errors": [{"code": "parse_failed"}]}],
    )
    monkeypatch.setattr(
        auto,
        "submit_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("no POST after incomplete strict scan")
        ),
    )

    result = auto.run_cycle(force=True)

    assert result["code"] == "strict_scan_incomplete"
    assert calls == [["claude"]]


def test_submitting_receipt_404_cannot_post_after_late_hold(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config)
    _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="submitting",
    )
    conn.close()
    monkeypatch.setattr(auto, "load_credentials", lambda **_kwargs: _credentials())
    _patch_runner_host(monkeypatch)

    def not_found(*_args, **_kwargs):
        raise RecurringServiceError(
            "receipt_not_found", "No receipt exists.", status=404
        )

    monkeypatch.setattr(auto, "lookup_receipt", not_found)

    def apply_late_hold():
        inner = open_index()
        try:
            set_hold_state(
                inner,
                "session-one",
                "pending_review",
                changed_by="test",
                reason="hold during recovery scan",
            )
        finally:
            inner.close()

    _patch_strict_scanner(monkeypatch, callbacks=[apply_late_hold])
    post_calls: list[str] = []
    monkeypatch.setattr(
        auto,
        "submit_artifact",
        lambda *_args, **_kwargs: post_calls.append("POST"),
    )

    result = auto.run_cycle(force=True)

    assert result["code"] == "control_changed"
    assert post_calls == []


def test_ambiguous_retry_reuses_exact_zip_submission_key_and_revision_keys(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    enrollment = _save_enabled_enrollment(conn, config)
    share_id, artifact_path = _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="sealed",
        client_submission_id="stable-idempotency-key",
    )
    _patch_runner_host(monkeypatch)
    attempts: list[dict[str, Any]] = []

    def submit_once_ambiguous(_capabilities, **kwargs):
        attempts.append(
            {
                "bytes": kwargs["artifact_path"].read_bytes(),
                "path": kwargs["artifact_path"],
                "client_submission_id": kwargs["client_submission_id"],
                "authorization_revision": kwargs["authorization_revision"],
                "trace_revision_keys": list(kwargs["trace_revision_keys"]),
                "artifact_sha256": kwargs["artifact_sha256"],
            }
        )
        raise RecurringServiceError(
            "server_unavailable",
            "Connection closed after request bytes were sent.",
            retryable=True,
            ambiguous=True,
        )

    monkeypatch.setattr(auto, "submit_artifact", submit_once_ambiguous)
    with pytest.raises(RecurringServiceError) as exc_info:
        auto._reconcile_pending(
            conn,
            enrollment=enrollment,
            credentials=_credentials(),
            capabilities=_capabilities(),
            allow_submit=True,
        )
    assert exc_info.value.ambiguous is True
    assert conn.execute(
        "SELECT submission_state FROM shares WHERE share_id = ?", (share_id,)
    ).fetchone()[0] == "submitting"

    monkeypatch.setattr(
        auto,
        "lookup_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RecurringServiceError("receipt_not_found", "not found", status=404)
        ),
    )

    def submit_retry(_capabilities, **kwargs):
        attempts.append(
            {
                "bytes": kwargs["artifact_path"].read_bytes(),
                "path": kwargs["artifact_path"],
                "client_submission_id": kwargs["client_submission_id"],
                "authorization_revision": kwargs["authorization_revision"],
                "trace_revision_keys": list(kwargs["trace_revision_keys"]),
                "artifact_sha256": kwargs["artifact_sha256"],
            }
        )
        return {
            "receipt_id": "receipt-idempotent",
            "accepted_at": "2026-07-15T11:00:00+00:00",
            "status": "accepted",
        }

    monkeypatch.setattr(auto, "submit_artifact", submit_retry)
    result = auto._reconcile_pending(
        conn,
        enrollment=enrollment,
        credentials=_credentials(),
        capabilities=_capabilities(),
        allow_submit=True,
    )

    assert result["code"] == "uploaded"
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert attempts[0]["path"] == artifact_path
    assert attempts[0]["bytes"] == b"exact-sealed-zip-bytes"
    assert attempts[0]["client_submission_id"] == "stable-idempotency-key"
    accepted = conn.execute(
        "SELECT submission_state, hosted_receipt_id FROM shares WHERE share_id = ?",
        (share_id,),
    ).fetchone()
    assert tuple(accepted) == ("accepted", "receipt-idempotent")
    conn.close()


def test_receipt_lookup_succeeds_when_ambiguous_artifact_bytes_are_missing(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    enrollment = _save_enabled_enrollment(conn, config)
    _, artifact_path = _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="submitting",
    )
    artifact_path.unlink()
    monkeypatch.setattr(
        auto,
        "lookup_receipt",
        lambda *_args, **_kwargs: {
            "receipt_id": "receipt-without-local-zip",
            "accepted_at": "2026-07-15T11:30:00+00:00",
            "status": "accepted",
        },
    )

    result = auto._reconcile_pending(
        conn,
        enrollment=enrollment,
        credentials=_credentials(),
        capabilities=_capabilities(),
        allow_submit=False,
    )

    assert result["code"] == "uploaded"
    assert result["receipt_reference"] == "receipt-without-local-zip"
    conn.close()


def _patch_enable_dependencies(monkeypatch, *, hook_result: bool = True):
    monkeypatch.setattr(auto, "_has_successful_manual_receipt", lambda _conn: True)
    _patch_strict_scanner(monkeypatch)
    monkeypatch.setattr(auto, "fetch_capabilities", lambda **_kwargs: _capabilities())
    monkeypatch.setattr(auto, "fetch_authorization", lambda _caps: _terms())
    monkeypatch.setattr(auto, "_snapshot_hook_files", lambda _targets: {})
    monkeypatch.setattr(
        auto,
        "install_hooks",
        lambda **_kwargs: [{"target": "claude", "configured": hook_result}],
    )
    monkeypatch.setattr(
        auto,
        "hook_diagnostics",
        lambda target, *, last_observed_at: {
            "target": target,
            "configured": hook_result,
            "observed": bool(last_observed_at),
        },
    )
    monkeypatch.setattr(auto, "uninstall_agent_hook", lambda _target: None)


def _current_authorization_profile_hash(*, agent: str = "claude") -> str:
    challenge = auto.enable(agent=agent, challenge_only=True)
    assert challenge["code"] == "authorization_required"
    return str(challenge["authorization_profile_hash"])


def _enrollment_response() -> dict[str, Any]:
    return {
        "enrollment_id": "server-enrollment-1",
        "enrolled_at": "2026-07-15T12:00:00+00:00",
        "authorization_revision": 1,
        **{
            key: value
            for key, value in _credentials().items()
            if key
            in {
                "active_token",
                "active_token_expires_at",
                "recovery_token",
                "recovery_token_expires_at",
            }
        },
    }


def test_enable_requires_exact_versions_then_commits_all_authority_transactionally(
    isolated_auto_upload,
    monkeypatch,
):
    _save_scope_config(upload_token="fresh-one-shot")
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    conn.close()
    _patch_enable_dependencies(monkeypatch)
    create_calls: list[dict[str, Any]] = []

    def create(_capabilities, **kwargs):
        create_calls.append(dict(kwargs))
        return _enrollment_response()

    monkeypatch.setattr(auto, "create_enrollment", create)

    challenge = auto.enable(agent="claude")

    assert challenge["status"] == 409
    assert challenge["code"] == "authorization_required"
    assert challenge["authorization"]["version"] == AUTH_VERSION
    assert challenge["retention"]["version"] == RETENTION_VERSION
    assert create_calls == []
    conn = open_index()
    try:
        assert get_auto_upload_enrollment(conn) is None
    finally:
        conn.close()
    assert not credential_path().exists()

    versions_only = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
    )
    assert versions_only["code"] == "authorization_required"
    assert create_calls == []

    result = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
        accepted_authorization_profile_hash=challenge["authorization_profile_hash"],
    )

    assert result["mode"] == "enabled"
    assert result["health"] == "ready"
    assert len(create_calls) == 1
    assert create_calls[0]["upload_token"] == "fresh-one-shot"
    assert load_credentials(required=True)["active_token"] == "active-secret"
    persisted = config_module.load_config()
    assert "verified_email_token" not in persisted
    assert persisted["auto_upload_capability_available"] is True
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["mode"] == "enabled"
        assert enrollment["server_enrollment_id"] == "server-enrollment-1"
        assert enrollment["hook_targets"] == ["claude"]
    finally:
        conn.close()


def test_enable_rejects_acceptance_when_displayed_scope_changes(
    isolated_auto_upload,
    monkeypatch,
):
    _save_scope_config(upload_token="fresh-one-shot")
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    conn.close()
    _patch_enable_dependencies(monkeypatch)
    create_calls: list[str] = []
    monkeypatch.setattr(
        auto,
        "create_enrollment",
        lambda *_args, **_kwargs: create_calls.append("create"),
    )

    displayed = auto.enable(agent="claude", challenge_only=True)
    assert displayed["scope"]["projects"] == ["project-one"]

    conn = open_index()
    try:
        session, _raw_path = _session(
            isolated_auto_upload["root"],
            "session-two",
            project="project-two",
        )
        assert upsert_sessions(conn, [session]) == 1
    finally:
        conn.close()

    result = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
        accepted_authorization_profile_hash=displayed["authorization_profile_hash"],
    )

    assert result["code"] == "authorization_required"
    assert result["scope"]["projects"] == ["project-one", "project-two"]
    assert (
        result["authorization_profile_hash"]
        != displayed["authorization_profile_hash"]
    )
    assert create_calls == []
    assert not credential_path().exists()
    conn = open_index()
    try:
        assert get_auto_upload_enrollment(conn) is None
    finally:
        conn.close()


def test_enable_never_backdates_cutoff_before_durable_local_intent(
    isolated_auto_upload,
    monkeypatch,
):
    _save_scope_config(upload_token="fresh-one-shot")
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    conn.close()
    _patch_enable_dependencies(monkeypatch)
    local_intent = datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(auto, "_now", lambda: local_intent)
    response = _enrollment_response()
    response["enrolled_at"] = "2026-07-15T12:00:00+00:00"
    monkeypatch.setattr(
        auto, "create_enrollment", lambda *_args, **_kwargs: response
    )

    result = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
        accepted_authorization_profile_hash=_current_authorization_profile_hash(),
    )

    assert result["mode"] == "enabled"
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["enrolled_at"] == local_intent.isoformat()
    finally:
        conn.close()


def test_new_enrollment_after_disable_resets_prior_cadence_and_receipt(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config(upload_token="fresh-reverification")
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(
        conn,
        config,
        enrolled_at="2026-06-01T00:00:00+00:00",
    )
    assert update_auto_upload_enrollment(
        conn,
        expected_generation=1,
        mode="off",
        generation=2,
        health="ready",
        revocation_pending=False,
        last_completed_at="2026-07-01T00:00:00+00:00",
        last_result_count=5,
        last_receipt_reference="receipt-prior-enrollment",
        last_result_code="disabled",
    )
    conn.close()
    _patch_enable_dependencies(monkeypatch)
    response = _enrollment_response()
    response["enrollment_id"] = "server-enrollment-2"
    monkeypatch.setattr(
        auto, "create_enrollment", lambda *_args, **_kwargs: response
    )

    result = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
        accepted_authorization_profile_hash=_current_authorization_profile_hash(),
    )

    assert result["mode"] == "enabled"
    assert result["generation"] == 3
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["server_enrollment_id"] == "server-enrollment-2"
        assert enrollment["last_completed_at"] is None
        assert enrollment["last_result_count"] is None
        assert enrollment["last_receipt_reference"] is None
        assert auto.due_decision(enrollment).due is False
    finally:
        conn.close()


def test_reauthorization_rejects_later_future_only_cutoff(
    isolated_auto_upload,
    monkeypatch,
):
    # Moving the boundary *later* than the committed one is an unexpected
    # forward move for a fixed enrollment and must be rejected.
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(
        conn,
        config,
        enrolled_at="2026-07-15T13:00:00+00:00",
    )
    conn.close()
    write_credentials(_credentials())
    _patch_enable_dependencies(monkeypatch)
    monkeypatch.setattr(
        auto,
        "update_enrollment",
        lambda *_args, **_kwargs: {
            "enrollment_id": "server-enrollment-1",
            "enrolled_at": "2026-07-15T14:00:00+00:00",
            "authorization_revision": 2,
        },
    )

    result = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
        accepted_authorization_profile_hash=_current_authorization_profile_hash(),
    )

    assert result["code"] == "malformed_enrollment_response"
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["mode"] == "paused"
        assert enrollment["health"] == "action_required"
        assert enrollment["enrolled_at"] == "2026-07-15T13:00:00+00:00"
    finally:
        conn.close()


def test_reauthorization_accepts_earlier_server_cutoff_from_clock_skew(
    isolated_auto_upload,
    monkeypatch,
):
    # The create clamp stores max(local_intent, server), so when the local
    # clock led the server at create time the stored boundary is later than the
    # server's own value. On reauthorization the server returns that earlier
    # original value; this must succeed and keep the stored (more conservative)
    # boundary, not fail reauth forever.
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(
        conn,
        config,
        enrolled_at="2026-07-15T13:00:00+00:00",
    )
    conn.close()
    write_credentials(_credentials())
    _patch_enable_dependencies(monkeypatch)
    monkeypatch.setattr(
        auto,
        "update_enrollment",
        lambda *_args, **_kwargs: {
            "enrollment_id": "server-enrollment-1",
            "enrolled_at": "2026-07-15T12:00:00+00:00",
            "authorization_revision": 2,
        },
    )

    result = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
        accepted_authorization_profile_hash=_current_authorization_profile_hash(),
    )

    assert result.get("code") != "malformed_enrollment_response"
    assert result.get("ok") is not False
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["mode"] == "enabled"
        assert enrollment["enrolled_at"] == "2026-07-15T13:00:00+00:00"
    finally:
        conn.close()


def test_non_rotating_update_preserves_unused_manual_verified_email_token(
    isolated_auto_upload,
    monkeypatch,
):
    # A non-rotating scope/terms update reuses the pinned active credential and
    # never sends verified_email_token, so it must not delete a still-valid
    # manual-share token the user re-verified after enrolling.
    config = _save_scope_config(upload_token="valid-manual-token")
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(
        conn,
        config,
        enrolled_at="2026-07-15T13:00:00+00:00",
    )
    conn.close()
    write_credentials(_credentials())
    _patch_enable_dependencies(monkeypatch)
    monkeypatch.setattr(
        auto,
        "update_enrollment",
        lambda *_args, **_kwargs: {
            "enrollment_id": "server-enrollment-1",
            "enrolled_at": "2026-07-15T13:00:00+00:00",
            "authorization_revision": 2,
        },
    )

    result = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
        accepted_authorization_profile_hash=_current_authorization_profile_hash(),
    )

    assert result.get("ok") is not False
    assert config_module.load_config().get("verified_email_token") == "valid-manual-token"


def test_reauthorization_discards_same_enrollment_sealed_artifact_after_patch(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config)
    share_id, artifact_path = _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="sealed",
    )
    conn.close()
    write_credentials(_credentials())
    _patch_enable_dependencies(monkeypatch)
    monkeypatch.setattr(
        auto,
        "update_enrollment",
        lambda *_args, **_kwargs: {
            "enrollment_id": "server-enrollment-1",
            "enrolled_at": ENROLLED_AT,
            "authorization_revision": 2,
        },
    )

    result = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
        accepted_authorization_profile_hash=_current_authorization_profile_hash(),
    )

    assert result["mode"] == "enabled"
    assert result["generation"] == 2
    assert not artifact_path.exists()
    conn = open_index()
    try:
        assert conn.execute(
            "SELECT 1 FROM shares WHERE share_id = ?", (share_id,)
        ).fetchone() is None
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["authorization_revision"] == 2
    finally:
        conn.close()


@pytest.mark.parametrize("rotating_credentials", [False, True])
@pytest.mark.parametrize("restore_fails", [False, True])
def test_pause_racing_successful_reauthorization_reconciles_hosted_revision(
    isolated_auto_upload,
    monkeypatch,
    restore_fails,
    rotating_credentials,
):
    config = _save_scope_config(upload_token="fresh-one-shot")
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config)
    assert update_auto_upload_enrollment(
        conn,
        expected_generation=1,
        recurring_authorization_version="authorization-old",
        retention_version="retention-old",
        egress_profile_hash="profile-old",
    )
    conn.close()
    stored_credentials = _credentials()
    if rotating_credentials:
        stored_credentials["active_token_expires_at"] = "2020-01-01T00:00:00+00:00"
    write_credentials(stored_credentials)
    _patch_enable_dependencies(monkeypatch)

    patch_succeeded = threading.Event()
    recovery_only_written = threading.Event()
    release_recovery_write = threading.Event()
    enable_results: list[dict[str, Any]] = []
    thread_errors: list[BaseException] = []
    real_write_credentials = auto.write_credentials

    def successful_reauthorization(*_args, **_kwargs):
        patch_succeeded.set()
        response = {
            "enrollment_id": "server-enrollment-1",
            "enrolled_at": ENROLLED_AT,
            "authorization_revision": 2,
        }
        if rotating_credentials:
            response.update({
                key: value
                for key, value in _credentials().items()
                if key in {
                    "active_token",
                    "active_token_expires_at",
                    "recovery_token",
                    "recovery_token_expires_at",
                }
            })
        return response

    def block_after_recovery_write(record):
        if record.get("active_token") is not None and restore_fails:
            raise CredentialStoreError("active credential restore failed")
        path = real_write_credentials(record)
        if record.get("active_token") is None:
            assert patch_succeeded.is_set()
            recovery_only_written.set()
            assert release_recovery_write.wait(timeout=5)
        return path

    def run_enable():
        try:
            enable_results.append(
                auto.enable(
                    agent="claude",
                    accepted_authorization_version=AUTH_VERSION,
                    accepted_retention_version=RETENTION_VERSION,
                    accepted_authorization_profile_hash=profile_hash,
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            thread_errors.append(exc)

    monkeypatch.setattr(
        auto,
        "create_enrollment" if rotating_credentials else "update_enrollment",
        successful_reauthorization,
    )
    monkeypatch.setattr(auto, "write_credentials", block_after_recovery_write)
    profile_hash = _current_authorization_profile_hash()
    enable_thread = threading.Thread(target=run_enable, name="reauthorize")
    enable_thread.start()
    assert recovery_only_written.wait(timeout=5), "recovery credential was not saved"
    assert load_credentials(required=True)["active_token"] is None

    paused = auto.pause()

    assert paused["mode"] == "paused"
    assert paused["generation"] == 3
    release_recovery_write.set()
    enable_thread.join(timeout=5)
    assert not enable_thread.is_alive()
    assert thread_errors == []
    assert len(enable_results) == 1
    if restore_fails:
        assert enable_results[0]["code"] == "credential_store_failed"
    else:
        assert enable_results[0]["mode"] == "paused"
        assert enable_results[0]["generation"] == 4

    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["mode"] == "paused"
        assert enrollment["health"] == (
            "action_required" if restore_fails else "ready"
        )
        assert enrollment["last_result_code"] == (
            "credential_store_failed" if restore_fails else "paused"
        )
        assert enrollment["authorization_revision"] == 2
        assert enrollment["recurring_authorization_version"] == AUTH_VERSION
        assert enrollment["retention_version"] == RETENTION_VERSION
        assert enrollment["egress_profile_hash"] != "profile-old"
        assert enrollment["hook_targets"] == ["claude"]
    finally:
        conn.close()
    assert load_credentials(required=True)["active_token"] == (
        None if restore_fails else "active-secret"
    )


def test_reauthorization_blocks_before_patch_while_receipt_is_ambiguous(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config)
    share_id, artifact_path = _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="submitting",
    )
    conn.close()
    write_credentials(_credentials())
    _patch_enable_dependencies(monkeypatch)
    patch_calls: list[str] = []
    monkeypatch.setattr(
        auto,
        "update_enrollment",
        lambda *_args, **_kwargs: patch_calls.append("PATCH"),
    )

    result = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
        accepted_authorization_profile_hash=_current_authorization_profile_hash(),
    )

    assert result["code"] == "receipt_reconciliation_pending"
    assert result["retryable"] is True
    assert patch_calls == []
    assert artifact_path.exists()
    conn = open_index()
    try:
        share = conn.execute(
            "SELECT submission_state FROM shares WHERE share_id = ?", (share_id,)
        ).fetchone()
        assert share["submission_state"] == "submitting"
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["generation"] == 1
        assert enrollment["authorization_revision"] == 1
    finally:
        conn.close()


def test_enable_snapshot_failure_returns_structured_error_and_stays_off(
    isolated_auto_upload,
    monkeypatch,
):
    _save_scope_config(upload_token="fresh-one-shot")
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    conn.close()
    _patch_enable_dependencies(monkeypatch)
    monkeypatch.setattr(
        auto,
        "_snapshot_hook_files",
        lambda _targets: (_ for _ in ()).throw(OSError("hook file unreadable")),
    )
    server_calls: list[str] = []
    monkeypatch.setattr(
        auto,
        "create_enrollment",
        lambda *_args, **_kwargs: server_calls.append("create"),
    )

    result = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
        accepted_authorization_profile_hash=_current_authorization_profile_hash(),
    )

    assert result["ok"] is False
    assert result["code"] == "enrollment_failed"
    assert server_calls == []
    assert not credential_path().exists()
    assert config_module.load_config()["verified_email_token"] == "fresh-one-shot"
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["mode"] == "off"
        assert enrollment["server_enrollment_id"] is None
        assert enrollment["revocation_pending"] is False
    finally:
        conn.close()


def test_enable_rolls_back_hook_failure_before_server_or_credentials(
    isolated_auto_upload,
    monkeypatch,
):
    _save_scope_config(upload_token="fresh-one-shot")
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    conn.close()
    _patch_enable_dependencies(monkeypatch, hook_result=False)
    restored: list[dict[Path, str | None]] = []
    monkeypatch.setattr(auto, "_restore_hook_files", lambda snapshot: restored.append(snapshot))
    monkeypatch.setattr(
        auto,
        "create_enrollment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("hook failure must precede server enrollment")
        ),
    )

    result = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
        accepted_authorization_profile_hash=_current_authorization_profile_hash(),
    )

    assert result["code"] == "hook_install_failed"
    assert restored == [{}]
    assert not credential_path().exists()
    assert config_module.load_config()["verified_email_token"] == "fresh-one-shot"
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["mode"] == "off"
        assert enrollment["server_enrollment_id"] is None
    finally:
        conn.close()


def test_enable_revokes_server_enrollment_when_credential_commit_fails(
    isolated_auto_upload,
    monkeypatch,
):
    _save_scope_config(upload_token="fresh-one-shot")
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    conn.close()
    _patch_enable_dependencies(monkeypatch)
    monkeypatch.setattr(auto, "create_enrollment", lambda *_args, **_kwargs: _enrollment_response())
    monkeypatch.setattr(
        auto,
        "write_credentials",
        lambda _record: (_ for _ in ()).throw(CredentialStoreError("disk full")),
    )
    revoked: list[tuple[str, str]] = []
    monkeypatch.setattr(
        auto,
        "revoke_enrollment",
        lambda _caps, *, enrollment_id, recovery_token: revoked.append(
            (enrollment_id, recovery_token)
        ),
    )

    result = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
        accepted_authorization_profile_hash=_current_authorization_profile_hash(),
    )

    assert result["code"] == "credential_store_failed"
    assert revoked == [("server-enrollment-1", "recovery-secret")]
    assert config_module.load_config()["verified_email_token"] == "fresh-one-shot"
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["mode"] == "off"
        assert enrollment["server_enrollment_id"] is None
    finally:
        conn.close()


def test_definitely_revoked_first_create_rotates_next_idempotency_key(
    isolated_auto_upload,
    monkeypatch,
):
    _save_scope_config(upload_token="fresh-one-shot")
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    conn.close()
    _patch_enable_dependencies(monkeypatch)

    create_ids: list[str] = []
    revoked: list[str] = []
    credential_writes = 0
    real_write_credentials = auto.write_credentials

    def create(_capabilities, **kwargs):
        create_ids.append(str(kwargs["client_enrollment_id"]))
        response = _enrollment_response()
        response["enrollment_id"] = f"server-enrollment-{len(create_ids)}"
        return response

    def fail_first_credential_write(record):
        nonlocal credential_writes
        credential_writes += 1
        if credential_writes == 1:
            raise CredentialStoreError("disk full")
        return real_write_credentials(record)

    monkeypatch.setattr(auto, "create_enrollment", create)
    monkeypatch.setattr(auto, "write_credentials", fail_first_credential_write)
    monkeypatch.setattr(
        auto,
        "revoke_enrollment",
        lambda _caps, *, enrollment_id, recovery_token: revoked.append(enrollment_id),
    )
    profile_hash = _current_authorization_profile_hash()

    failed = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
        accepted_authorization_profile_hash=profile_hash,
    )

    assert failed["code"] == "credential_store_failed"
    assert revoked == ["server-enrollment-1"]
    conn = open_index()
    try:
        after_revoke = get_auto_upload_enrollment(conn)
        assert after_revoke["client_enrollment_id"] != create_ids[0]
        rotated_id = after_revoke["client_enrollment_id"]
    finally:
        conn.close()

    enabled = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
        accepted_authorization_profile_hash=profile_hash,
    )

    assert enabled["mode"] == "enabled"
    assert create_ids == [create_ids[0], rotated_id]
    assert create_ids[0] != create_ids[1]
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["client_enrollment_id"] == create_ids[1]
        assert enrollment["server_enrollment_id"] == "server-enrollment-2"
    finally:
        conn.close()


def test_enable_generation_race_cannot_commit_or_restore_over_newer_controls(
    isolated_auto_upload,
    monkeypatch,
):
    _save_scope_config(upload_token="fresh-one-shot")
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    conn.close()
    _patch_enable_dependencies(monkeypatch)
    monkeypatch.setattr(auto, "create_enrollment", lambda *_args, **_kwargs: _enrollment_response())
    real_write = auto.write_credentials

    def write_then_race(record):
        path = real_write(record)
        racer = open_index()
        try:
            current = get_auto_upload_enrollment(racer)
            assert update_auto_upload_enrollment(
                racer,
                expected_generation=current["generation"],
                generation=current["generation"] + 1,
                mode="off",
                health="action_required",
                last_result_code="newer-user-control",
            )
        finally:
            racer.close()
        return path

    monkeypatch.setattr(auto, "write_credentials", write_then_race)
    restored: list[Any] = []
    monkeypatch.setattr(auto, "_restore_hook_files", lambda snapshot: restored.append(snapshot))
    revoked: list[str] = []
    monkeypatch.setattr(
        auto,
        "revoke_enrollment",
        lambda _caps, *, enrollment_id, recovery_token: revoked.append(enrollment_id),
    )

    result = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
        accepted_authorization_profile_hash=_current_authorization_profile_hash(),
    )

    assert result["code"] == "control_changed"
    assert restored == []
    assert revoked == ["server-enrollment-1"]
    assert not credential_path().exists()
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["mode"] == "off"
        assert enrollment["last_result_code"] == "newer-user-control"
    finally:
        conn.close()


@pytest.mark.parametrize("rollback_revoke_fails", [False, True])
def test_disable_cancels_first_enable_while_create_request_is_in_flight(
    isolated_auto_upload,
    monkeypatch,
    rollback_revoke_fails,
):
    _save_scope_config(upload_token="fresh-one-shot")
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    conn.close()
    _patch_enable_dependencies(monkeypatch)

    create_started = threading.Event()
    allow_create_response = threading.Event()
    enable_results: list[dict[str, Any]] = []
    enable_errors: list[BaseException] = []
    credential_writes: list[dict[str, Any]] = []
    revoke_attempts: list[tuple[str, str]] = []
    real_write_credentials = auto.write_credentials

    def blocked_create(*_args, **_kwargs):
        create_started.set()
        assert allow_create_response.wait(timeout=5), "test did not release create request"
        return _enrollment_response()

    def record_credential_write(record):
        credential_writes.append(dict(record))
        return real_write_credentials(record)

    def revoke(_caps, *, enrollment_id, recovery_token):
        revoke_attempts.append((enrollment_id, recovery_token))
        if rollback_revoke_fails and len(revoke_attempts) == 1:
            raise RecurringServiceError(
                "network_error", "temporary revoke failure", retryable=True
            )

    def run_enable():
        try:
            enable_results.append(
                auto.enable(
                    agent="claude",
                    accepted_authorization_version=AUTH_VERSION,
                    accepted_retention_version=RETENTION_VERSION,
                    accepted_authorization_profile_hash=_current_authorization_profile_hash(),
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            enable_errors.append(exc)

    monkeypatch.setattr(auto, "create_enrollment", blocked_create)
    monkeypatch.setattr(auto, "write_credentials", record_credential_write)
    monkeypatch.setattr(auto, "revoke_enrollment", revoke)
    monkeypatch.setattr(
        auto, "recovery_capabilities", lambda origin: _capabilities(origin)
    )

    enable_thread = threading.Thread(target=run_enable, name="enable-request")
    enable_thread.start()
    assert create_started.wait(timeout=5), "enable did not reach hosted create"

    pending_conn = open_index()
    try:
        pending = get_auto_upload_enrollment(pending_conn)
        assert pending["mode"] == "off"
        assert pending["generation"] == 1
        assert pending["last_result_code"] == "enrollment_pending"
    finally:
        pending_conn.close()

    disabled = auto.disable()

    assert disabled["mode"] == "off"
    assert disabled["generation"] == 2
    assert disabled["overlay"] is None
    assert disabled["last_result"]["code"] == "disabled"
    allow_create_response.set()
    enable_thread.join(timeout=5)
    assert not enable_thread.is_alive()

    assert enable_errors == []
    assert len(enable_results) == 1
    assert enable_results[0]["code"] == "control_changed"
    assert revoke_attempts == [("server-enrollment-1", "recovery-secret")]
    if rollback_revoke_fails:
        assert len(credential_writes) == 1
        assert credential_writes[0]["active_token"] is None
        assert credential_writes[0]["active_token_expires_at"] is None
        tombstone = load_credentials(required=True)
        assert tombstone["active_token"] is None
        assert tombstone["recovery_token"] == "recovery-secret"
        pending_conn = open_index()
        try:
            pending_revoke = get_auto_upload_enrollment(pending_conn)
            assert pending_revoke["mode"] == "off"
            assert pending_revoke["generation"] == 3
            assert pending_revoke["revocation_pending"] is True
            assert pending_revoke["last_result_code"] == "revocation_pending"
        finally:
            pending_conn.close()

        recovered = auto.disable()

        assert recovered["mode"] == "off"
        assert recovered["generation"] == 4
        assert recovered["overlay"] is None
        assert revoke_attempts == [
            ("server-enrollment-1", "recovery-secret"),
            ("server-enrollment-1", "recovery-secret"),
        ]
    else:
        assert credential_writes == []
    assert not credential_path().exists()
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["mode"] == "off"
        assert enrollment["generation"] == (4 if rollback_revoke_fails else 2)
        assert enrollment["revocation_pending"] is False
        assert enrollment["last_result_code"] == "disabled"
        if not rollback_revoke_fails:
            assert enrollment["server_enrollment_id"] is None
    finally:
        conn.close()


def test_disable_waits_for_authority_handoff_and_recovers_after_process_death(
    isolated_auto_upload,
    monkeypatch,
):
    _save_scope_config(upload_token="fresh-one-shot")
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    conn.close()
    _patch_enable_dependencies(monkeypatch)
    monkeypatch.setattr(
        auto, "create_enrollment", lambda *_args, **_kwargs: _enrollment_response()
    )
    monkeypatch.setattr(
        auto, "recovery_capabilities", lambda origin: _capabilities(origin)
    )

    class SimulatedProcessDeath(BaseException):
        pass

    active_write_started = threading.Event()
    release_active_write = threading.Event()
    enable_errors: list[BaseException] = []
    disable_results: list[dict[str, Any]] = []
    observations: list[tuple[str | None, str]] = []
    revoke_attempts: list[tuple[str, str]] = []
    real_write_credentials = auto.write_credentials

    def crash_during_active_write(record):
        observation_conn = open_index()
        try:
            enrollment = get_auto_upload_enrollment(observation_conn)
            observations.append((record.get("active_token"), enrollment["mode"]))
        finally:
            observation_conn.close()
        if record.get("active_token"):
            active_write_started.set()
            assert release_active_write.wait(timeout=5)
            raise SimulatedProcessDeath()
        return real_write_credentials(record)

    def run_enable():
        try:
            auto.enable(
                agent="claude",
                accepted_authorization_version=AUTH_VERSION,
                accepted_retention_version=RETENTION_VERSION,
                accepted_authorization_profile_hash=_current_authorization_profile_hash(),
            )
        except BaseException as exc:  # expected simulated process death
            enable_errors.append(exc)

    def run_disable():
        disable_results.append(auto.disable())

    monkeypatch.setattr(auto, "write_credentials", crash_during_active_write)
    monkeypatch.setattr(
        auto,
        "revoke_enrollment",
        lambda _caps, *, enrollment_id, recovery_token: revoke_attempts.append(
            (enrollment_id, recovery_token)
        ),
    )

    enable_thread = threading.Thread(target=run_enable, name="enable-crash")
    enable_thread.start()
    assert active_write_started.wait(timeout=5)

    persisted = load_credentials(required=True)
    assert persisted["active_token"] is None
    assert persisted["recovery_token"] == "recovery-secret"

    disable_thread = threading.Thread(target=run_disable, name="disable-after-crash")
    disable_thread.start()
    # Disable must wait for the active-write authority phase, not race past it
    # with a stale credential snapshot.
    disable_thread.join(timeout=0.1)
    assert disable_thread.is_alive()

    release_active_write.set()
    enable_thread.join(timeout=5)
    disable_thread.join(timeout=5)
    assert not enable_thread.is_alive()
    assert not disable_thread.is_alive()
    assert len(enable_errors) == 1
    assert isinstance(enable_errors[0], SimulatedProcessDeath)
    assert observations == [
        (None, "off"),
        ("active-secret", "enabled"),
    ]
    assert revoke_attempts == [("server-enrollment-1", "recovery-secret")]
    assert len(disable_results) == 1
    assert disable_results[0]["mode"] == "off"
    assert disable_results[0]["generation"] == 2
    assert disable_results[0]["last_result"]["code"] == "disabled"
    assert not credential_path().exists()


def test_stale_disable_cannot_erase_later_rollback_recovery_handoff(
    isolated_auto_upload,
    monkeypatch,
):
    _save_scope_config(upload_token="fresh-one-shot")
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    conn.close()
    _patch_enable_dependencies(monkeypatch)

    create_started = threading.Event()
    allow_create_response = threading.Event()
    disable_in_hook_cleanup = threading.Event()
    allow_disable_to_finish = threading.Event()
    rollback_revoke_failed = threading.Event()
    enable_results: list[dict[str, Any]] = []
    disable_results: list[dict[str, Any]] = []
    thread_errors: list[BaseException] = []
    revoke_attempts: list[tuple[str, str]] = []

    def blocked_create(*_args, **_kwargs):
        create_started.set()
        assert allow_create_response.wait(timeout=5)
        return _enrollment_response()

    def block_stale_disable_hook(_target):
        if (
            threading.current_thread().name == "stale-disable"
            and not disable_in_hook_cleanup.is_set()
        ):
            disable_in_hook_cleanup.set()
            assert allow_disable_to_finish.wait(timeout=5)

    def revoke(_caps, *, enrollment_id, recovery_token):
        revoke_attempts.append((enrollment_id, recovery_token))
        if threading.current_thread().name == "enable-request":
            rollback_revoke_failed.set()
            raise RecurringServiceError(
                "network_error", "temporary revoke failure", retryable=True
            )

    def run_enable():
        try:
            enable_results.append(
                auto.enable(
                    agent="claude",
                    accepted_authorization_version=AUTH_VERSION,
                    accepted_retention_version=RETENTION_VERSION,
                    accepted_authorization_profile_hash=_current_authorization_profile_hash(),
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            thread_errors.append(exc)

    def run_disable():
        try:
            disable_results.append(auto.disable())
        except BaseException as exc:  # pragma: no cover - asserted below
            thread_errors.append(exc)

    monkeypatch.setattr(auto, "create_enrollment", blocked_create)
    monkeypatch.setattr(auto, "uninstall_agent_hook", block_stale_disable_hook)
    monkeypatch.setattr(auto, "revoke_enrollment", revoke)
    monkeypatch.setattr(
        auto, "recovery_capabilities", lambda origin: _capabilities(origin)
    )

    enable_thread = threading.Thread(target=run_enable, name="enable-request")
    enable_thread.start()
    assert create_started.wait(timeout=5)

    disable_thread = threading.Thread(target=run_disable, name="stale-disable")
    disable_thread.start()
    assert disable_in_hook_cleanup.wait(timeout=5)

    allow_create_response.set()
    assert rollback_revoke_failed.wait(timeout=5)

    deadline = time.monotonic() + 5
    while True:
        pending_conn = open_index()
        try:
            pending = get_auto_upload_enrollment(pending_conn)
        finally:
            pending_conn.close()
        if pending["generation"] == 3 and pending["revocation_pending"]:
            break
        assert time.monotonic() < deadline, "rollback recovery handoff not persisted"
        time.sleep(0.01)

    tombstone = load_credentials(required=True)
    assert tombstone["active_token"] is None
    assert tombstone["recovery_token"] == "recovery-secret"

    allow_disable_to_finish.set()
    enable_thread.join(timeout=5)
    disable_thread.join(timeout=5)
    assert not enable_thread.is_alive()
    assert not disable_thread.is_alive()
    assert thread_errors == []
    assert enable_results[0]["code"] == "control_changed"
    assert disable_results[0]["generation"] == 3
    assert disable_results[0]["overlay"] == "revocation_pending"
    assert disable_results[0]["last_result"]["code"] == "revocation_pending"
    assert load_credentials(required=True)["recovery_token"] == "recovery-secret"

    recovered = auto.disable()

    assert recovered["mode"] == "off"
    assert recovered["generation"] == 4
    assert recovered["overlay"] is None
    assert recovered["last_result"]["code"] == "disabled"
    assert revoke_attempts == [
        ("server-enrollment-1", "recovery-secret"),
        ("server-enrollment-1", "recovery-secret"),
    ]
    assert not credential_path().exists()


def test_reauthorization_cannot_resurrect_active_token_after_disable_wins(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config)
    conn.close()
    write_credentials(_credentials())
    _patch_enable_dependencies(monkeypatch)
    monkeypatch.setattr(
        auto,
        "update_enrollment",
        lambda *_args, **_kwargs: {
            "enrollment_id": "server-enrollment-1",
            "enrolled_at": ENROLLED_AT,
            "authorization_revision": 2,
        },
    )
    real_write = auto.write_credentials

    def write_then_disable(record):
        path = real_write(record)
        racer = open_index()
        try:
            current = get_auto_upload_enrollment(racer)
            assert update_auto_upload_enrollment(
                racer,
                expected_generation=current["generation"],
                generation=current["generation"] + 1,
                mode="off",
                health="retrying",
                revocation_pending=True,
                last_result_code="revocation_pending",
            )
        finally:
            racer.close()
        return path

    monkeypatch.setattr(auto, "write_credentials", write_then_disable)

    result = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
        accepted_authorization_profile_hash=_current_authorization_profile_hash(),
    )

    assert result["code"] == "control_changed"
    tombstone = load_credentials(required=True)
    assert tombstone["active_token"] is None
    assert tombstone["active_token_expires_at"] is None
    assert tombstone["recovery_token"] == "recovery-secret"
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["mode"] == "off"
        assert enrollment["revocation_pending"] is True
        assert enrollment["generation"] == 3
    finally:
        conn.close()


def test_nothing_new_advances_successful_weekly_cadence(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(
        conn,
        config,
        enrolled_at="2026-07-10T00:00:00+00:00",
    )
    shared_id = create_share(conn, ["session-one"])
    conn.execute(
        "UPDATE shares SET status = 'shared', shared_at = ? WHERE share_id = ?",
        ("2026-07-14T00:00:00+00:00", shared_id),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(auto, "load_credentials", lambda **_kwargs: _credentials())
    hosted_calls: list[str] = []

    def hosted_forbidden(*_args, **_kwargs):
        hosted_calls.append("hosted")
        raise AssertionError("nothing_new must remain entirely local")

    monkeypatch.setattr(auto, "fetch_capabilities", hosted_forbidden)
    monkeypatch.setattr(auto, "get_enrollment", hosted_forbidden)
    monkeypatch.setattr(auto, "submit_artifact", hosted_forbidden)
    calls = _patch_strict_scanner(monkeypatch)

    before = datetime.now(timezone.utc)
    result = auto.run_cycle(force=True)
    after = datetime.now(timezone.utc)

    assert result == {"ok": True, "code": "nothing_new", "count": 0}
    assert calls == [["claude"]]
    assert hosted_calls == []
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        completed = auto._parse_time(enrollment["last_completed_at"])
        assert before <= completed <= after
        assert enrollment["last_result_code"] == "nothing_new"
        assert enrollment["last_result_count"] == 0
        assert enrollment["next_retry_at"] is None
        assert auto._due_at(enrollment) == completed + timedelta(days=7)
    finally:
        conn.close()


def test_runner_happy_path_seals_exact_artifact_and_commits_hosted_receipt(
    isolated_auto_upload,
    monkeypatch,
):
    """Exercise the real scan, candidate, package, ledger, and cadence path."""

    from clawjournal.parsing import parser as parser_module
    from clawjournal.workbench.daemon import Scanner

    root = isolated_auto_upload["root"]
    home = root / "home"
    monkeypatch.setenv("HOME", str(home))
    parser_paths = {
        "CLAUDE_DIR": home / ".claude",
        "PROJECTS_DIR": home / ".claude" / "projects",
        "LOCAL_AGENT_DIR": home / ".claude-desktop" / "local-agent-mode-sessions",
        "CLAUDE_SCIENCE_DIR": home / ".claude-science",
        "CODEX_DIR": home / ".codex",
        "CODEX_SESSIONS_DIR": home / ".codex" / "sessions",
        "CODEX_ARCHIVED_DIR": home / ".codex" / "archived_sessions",
        "GEMINI_DIR": home / ".gemini" / "tmp",
        "OPENCODE_DIR": home / ".local" / "share" / "opencode",
        "OPENCODE_DB_PATH": home / ".local" / "share" / "opencode" / "opencode.db",
        "OPENCLAW_DIR": home / ".openclaw",
        "OPENCLAW_AGENTS_DIR": home / ".openclaw" / "agents",
        "KIMI_DIR": home / ".kimi",
        "KIMI_SESSIONS_DIR": home / ".kimi" / "sessions",
        "KIMI_CONFIG_PATH": home / ".kimi" / "kimi.json",
        "CURSOR_DIR": home / ".cursor",
        "COPILOT_DIR": home / ".copilot" / "session-state",
        "WORKBUDDY_DIR": home / "WorkBuddy",
        "WORKBUDDY_AI_PROJECTS_DIR": home / ".workbuddy-ai" / "projects",
        "WORKBUDDY_IMPORT_DIR": home / ".clawjournal" / "workbuddy",
        "CUSTOM_DIR": home / ".clawjournal" / "custom",
    }
    for name, path in parser_paths.items():
        monkeypatch.setattr(parser_module, name, path)

    raw_path = (
        parser_paths["PROJECTS_DIR"]
        / "-workspace-project-one"
        / "session-happy.jsonl"
    )
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-07-12T08:00:00Z",
                "cwd": "/workspace/project-one",
                "message": {"content": "prepare the weekly report"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-07-12T09:00:00Z",
                "message": {
                    "model": "claude-test",
                    "content": [{"type": "text", "text": "report complete"}],
                    "usage": {"input_tokens": 5, "output_tokens": 2},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    config = _save_scope_config()
    initial_scan = Scanner(source_filter="claude").scan_once_strict(["claude"])
    assert initial_scan["ok"] is True
    assert initial_scan["new_by_source"] == {"claude": 1}

    conn = open_index()
    try:
        session = conn.execute(
            "SELECT session_id, project, content_revision, raw_source_path "
            "FROM sessions"
        ).fetchone()
        assert session is not None
        session_id = str(session["session_id"])
        revision = str(session["content_revision"])
        assert session["project"] == "claude:project-one"
        assert session["raw_source_path"] == str(raw_path)
        conn.execute(
            "UPDATE sessions SET revision_stable_since = ? WHERE session_id = ?",
            ("2026-07-12T09:00:00+00:00", session_id),
        )
        conn.commit()
        assert set_hold_state(
            conn,
            session_id,
            "released",
            changed_by="test",
            reason="fixture",
        )
        enrollment = _save_enabled_enrollment(
            conn,
            config,
            enrolled_at="2026-07-10T00:00:00+00:00",
            enrolled_projects=["claude:project-one"],
        )
        candidate_report = auto._candidate_report(conn, enrollment)
        assert [row["session_id"] for row in candidate_report["selected"]] == [
            session_id
        ]
    finally:
        conn.close()

    from clawjournal.redaction import trufflehog

    monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
    monkeypatch.setattr(trufflehog, "is_available", lambda: True)
    monkeypatch.setattr(
        trufflehog,
        "scan_file",
        lambda path: trufflehog.TruffleHogReport(
            scanned_path=str(path),
            scanned_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        ),
    )
    monkeypatch.setattr(trufflehog, "_scan_text_for_raw_matches", lambda _text: [])
    monkeypatch.setattr(
        trufflehog, "engine_fingerprint", lambda: "trufflehog test-clean"
    )
    write_credentials(_credentials())
    capabilities = _capabilities()
    remote_enrollment = {
        "enrollment_id": "server-enrollment-1",
        "submissions_open": True,
        "terms_current": True,
        "authorization_revision": 1,
        "authorization_version": AUTH_VERSION,
        "retention_policy_version": RETENTION_VERSION,
        "scope_hash": auto.scope_hash(["claude"], ["claude:project-one"]),
        "revoked_at": None,
    }
    monkeypatch.setattr(auto, "fetch_capabilities", lambda **_kwargs: capabilities)
    monkeypatch.setattr(
        auto, "get_enrollment", lambda *_args, **_kwargs: remote_enrollment
    )
    submissions: list[dict[str, Any]] = []

    def submit(_capabilities, **kwargs):
        artifact_bytes = kwargs["artifact_path"].read_bytes()
        with zipfile.ZipFile(io.BytesIO(artifact_bytes)) as archive:
            transport_manifest = json.loads(archive.read("manifest.json"))
            transported_sessions = [
                json.loads(line)
                for line in archive.read("sessions.jsonl").decode("utf-8").splitlines()
                if line.strip()
            ]
        submissions.append(
            {
                **kwargs,
                "artifact_bytes": artifact_bytes,
                "transport_manifest": transport_manifest,
                "transported_sessions": transported_sessions,
            }
        )
        return {
            "receipt_id": "receipt-happy-path",
            "accepted_at": "2026-07-15T14:00:00+00:00",
            "status": "accepted",
        }

    monkeypatch.setattr(auto, "submit_artifact", submit)

    before = datetime.now(timezone.utc)
    result = auto.run_cycle(force=True)
    after = datetime.now(timezone.utc)

    assert result["ok"] is True
    assert result["code"] == "uploaded"
    assert result["count"] == 1
    assert result["receipt_reference"] == "receipt-happy-path"
    assert len(submissions) == 1
    submitted = submissions[0]
    assert submitted["client_submission_id"] == result["client_submission_id"]
    assert submitted["authorization_revision"] == 1
    assert submitted["trace_revision_keys"] == [
        auto.trace_revision_key(session_id, revision)
    ]
    assert submitted["artifact_sha256"] == result["artifact_sha256"]
    assert submitted["artifact_sha256"] == hashlib.sha256(
        submitted["artifact_bytes"]
    ).hexdigest()
    assert submitted["transport_manifest"]["session_count"] == 1
    assert "export_path" not in submitted["transport_manifest"]
    assert [row["session_id"] for row in submitted["transported_sessions"]] == [
        session_id
    ]

    artifact_path = Path(submitted["artifact_path"])
    local_manifest = json.loads((artifact_path.parent / "manifest.json").read_text())
    assert local_manifest["export_path"] == str(artifact_path.parent)

    conn = open_index()
    try:
        share = conn.execute(
            "SELECT share_id, status, shared_at, hosted_receipt_id, hosted_status, "
            "submission_channel, enrollment_id, client_submission_id, "
            "authorization_revision, submission_state, sealed_artifact_sha256, "
            "sealed_artifact_path, sealed_raw_fingerprints FROM shares"
        ).fetchone()
        assert share is not None
        assert share["status"] == "shared"
        assert share["shared_at"] == "2026-07-15T14:00:00+00:00"
        assert share["hosted_receipt_id"] == "receipt-happy-path"
        assert share["hosted_status"] == "accepted"
        assert share["submission_channel"] == "auto_weekly"
        assert share["enrollment_id"] == "server-enrollment-1"
        assert share["client_submission_id"] == result["client_submission_id"]
        assert share["authorization_revision"] == 1
        assert share["submission_state"] == "accepted"
        assert share["sealed_artifact_sha256"] == result["artifact_sha256"]
        assert share["sealed_artifact_path"] == str(artifact_path)
        sealed_fingerprints = json.loads(share["sealed_raw_fingerprints"])
        assert list(sealed_fingerprints) == [session_id]
        shared_revision = conn.execute(
            "SELECT session_id, content_revision FROM share_sessions "
            "WHERE share_id = ?",
            (share["share_id"],),
        ).fetchone()
        assert tuple(shared_revision) == (session_id, revision)

        completed_enrollment = get_auto_upload_enrollment(conn)
        completed_at = auto._parse_time(completed_enrollment["last_completed_at"])
        assert before <= completed_at <= after
        assert completed_enrollment["last_result_code"] == "uploaded"
        assert completed_enrollment["last_result_count"] == 1
        assert completed_enrollment["last_receipt_reference"] == "receipt-happy-path"
        assert completed_enrollment["next_retry_at"] is None
        assert auto._due_at(completed_enrollment) == completed_at + timedelta(days=7)

        post_report = auto._candidate_report(conn, completed_enrollment)
        assert post_report["selected"] == []
        assert post_report["exclusion_counts"]["already_shared"] == 1
    finally:
        conn.close()


def test_runner_rejects_append_between_strict_parse_and_initial_fingerprint(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    raw_path = _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(
        conn,
        config,
        enrolled_at="2026-07-10T00:00:00+00:00",
    )
    conn.close()
    write_credentials(_credentials())
    _patch_runner_host(monkeypatch)
    parsed_fingerprint = auto._raw_fingerprints([
        {"session_id": "session-one", "raw_source_path": str(raw_path)}
    ])
    _patch_strict_scanner(
        monkeypatch,
        results=[
            {"ok": True},
            {
                "ok": True,
                "raw_fingerprints": {
                    key: list(value) for key, value in parsed_fingerprint.items()
                },
            },
        ],
    )
    original_prefix = auto._ranked_size_prefix
    appended = False

    def append_after_parse(*args, **kwargs):
        nonlocal appended
        result = original_prefix(*args, **kwargs)
        if not appended:
            with raw_path.open("a", encoding="utf-8") as handle:
                handle.write('{"late":"append"}\n')
            appended = True
        return result

    monkeypatch.setattr(auto, "_ranked_size_prefix", append_after_parse)
    post_calls: list[str] = []
    monkeypatch.setattr(
        auto,
        "submit_artifact",
        lambda *_args, **_kwargs: post_calls.append("POST"),
    )

    result = auto.run_cycle(force=True)

    assert result["code"] == "control_changed"
    assert post_calls == []
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["last_completed_at"] is None
    finally:
        conn.close()


def test_unmappable_trufflehog_finding_requires_action_instead_of_retry_loop(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(
        conn,
        config,
        enrolled_at="2026-07-10T00:00:00+00:00",
    )
    conn.close()
    write_credentials(_credentials())
    _patch_runner_host(monkeypatch)
    _patch_strict_scanner(monkeypatch)

    def blocked_package(conn, session_ids, _settings, **kwargs):
        share_id = create_share(
            conn,
            session_ids,
            expected_revisions=kwargs["expected_revisions"],
        )
        return {
            "ok": False,
            "share_id": share_id,
            "error": "TruffleHog found a secret without a safe line mapping.",
            "block_reason": "trufflehog-findings",
            "blocked_sessions": [],
        }

    monkeypatch.setattr(auto, "package", blocked_package)
    post_calls: list[str] = []
    monkeypatch.setattr(
        auto,
        "submit_artifact",
        lambda *_args, **_kwargs: post_calls.append("POST"),
    )

    result = auto.run_cycle(force=True)

    assert result["code"] == "unmappable_findings"
    assert result["retryable"] is False
    assert post_calls == []
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["mode"] == "enabled"
        assert enrollment["health"] == "action_required"
        assert enrollment["last_completed_at"] is None
        assert enrollment["last_result_code"] == "unmappable_findings"
        assert conn.execute("SELECT 1 FROM shares").fetchone() is None
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("force", "scheduled_client"),
    [(False, "claude"), (True, None)],
)
def test_action_required_blocks_scheduled_and_explicit_cycles_before_work(
    isolated_auto_upload,
    monkeypatch,
    force,
    scheduled_client,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(
        conn,
        config,
        health="action_required",
        enrolled_at="2026-07-01T00:00:00+00:00",
    )
    conn.close()
    forbidden_calls: list[str] = []

    def forbidden(*_args, **_kwargs):
        forbidden_calls.append("work")
        raise AssertionError("action_required must stop before scan, AI, or network")

    monkeypatch.setattr(auto, "load_credentials", forbidden)
    monkeypatch.setattr(auto, "fetch_capabilities", forbidden)
    monkeypatch.setattr(auto, "get_enrollment", forbidden)
    monkeypatch.setattr(auto, "lookup_receipt", forbidden)
    monkeypatch.setattr(auto, "submit_artifact", forbidden)
    monkeypatch.setattr(auto, "package", forbidden)

    result = auto.run_cycle(force=force, scheduled_client=scheduled_client)

    assert result["ok"] is False
    assert result["code"] == "action_required"
    assert forbidden_calls == []


def test_missing_selected_hook_blocks_scheduled_work_but_not_run_now(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(
        conn,
        config,
        enrolled_at="2026-07-13T00:00:00+00:00",
    )
    conn.close()
    work_calls: list[str] = []

    def forbidden(*_args, **_kwargs):
        work_calls.append("work")
        raise AssertionError("a scheduled missing-hook cycle must stop before work")

    monkeypatch.setattr(auto, "load_credentials", forbidden)
    scheduled = auto.run_cycle(force=False, scheduled_client="claude")

    assert scheduled["ok"] is False
    assert scheduled["code"] == "hook_missing"
    assert work_calls == []

    monkeypatch.setattr(auto, "load_credentials", lambda **_kwargs: _credentials())
    strict_calls = _patch_strict_scanner(monkeypatch)
    explicit = auto.run_cycle(force=True)

    assert explicit == {"ok": True, "code": "nothing_new", "count": 0}
    assert strict_calls == [["claude"]]
    assert auto.status()["health"] == "action_required"
    assert auto.status()["run_now_allowed"] is True


def test_action_required_sealed_artifact_does_not_lookup_or_post(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config, health="action_required")
    _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="sealed",
    )
    conn.close()
    calls: list[str] = []

    def forbidden(*_args, **_kwargs):
        calls.append("egress")
        raise AssertionError("sealed is not eligible for action-required recovery")

    monkeypatch.setattr(auto, "load_credentials", forbidden)
    monkeypatch.setattr(auto, "lookup_receipt", forbidden)
    monkeypatch.setattr(auto, "submit_artifact", forbidden)

    result = auto.run_cycle(force=True)

    assert result["code"] == "action_required"
    assert calls == []


def test_sealed_backoff_is_respected_by_hook_and_runner(
    isolated_auto_upload,
    monkeypatch,
):
    # A retryable submit failure leaves the share 'sealed' with next_retry_at
    # stamped. Neither the SessionStart due-check nor the runner may relaunch a
    # full cycle before that backoff elapses.
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config, enrolled_at="2026-07-01T00:00:00+00:00")
    _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="sealed",
    )
    retry_at = datetime.now(timezone.utc) + timedelta(hours=6)
    assert update_auto_upload_enrollment(
        conn,
        expected_generation=1,
        health="retrying",
        consecutive_failures=1,
        next_retry_at=retry_at.isoformat(),
    )
    conn.close()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("backoff must prevent any egress before next_retry_at")

    monkeypatch.setattr(auto, "load_credentials", forbidden)
    monkeypatch.setattr(auto, "lookup_receipt", forbidden)
    monkeypatch.setattr(auto, "submit_artifact", forbidden)

    # Hook due-check backs off while retry_at is in the future...
    assert auto.hook_session_start_check(
        "claude", retry_at - timedelta(hours=1)
    ) == agent_hooks.DueDecision(False, "retry-wait")
    # ...and becomes due once it elapses.
    assert auto.hook_session_start_check(
        "claude", retry_at + timedelta(hours=1)
    ) == agent_hooks.DueDecision(True, "retry-due")

    # A scheduled runner started during backoff exits without any egress call.
    result = auto.run_cycle(force=False)
    assert result["code"] == "retry-wait"


def test_submitting_pending_stays_due_despite_backoff(isolated_auto_upload):
    # A 'submitting' artifact may already have crossed egress; its receipt must
    # be reconciled promptly regardless of next_retry_at, so it stays due.
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config, enrolled_at="2026-07-01T00:00:00+00:00")
    _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="submitting",
    )
    retry_at = datetime.now(timezone.utc) + timedelta(hours=6)
    assert update_auto_upload_enrollment(
        conn,
        expected_generation=1,
        health="retrying",
        consecutive_failures=1,
        next_retry_at=retry_at.isoformat(),
    )
    conn.close()

    assert auto.hook_session_start_check(
        "claude", retry_at - timedelta(hours=1)
    ) == agent_hooks.DueDecision(True, "receipt-recovery-pending")


def test_action_required_submitting_artifact_allows_receipt_lookup_only(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config, health="action_required")
    share_id, _ = _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="submitting",
    )
    conn.close()
    monkeypatch.setattr(auto, "load_credentials", lambda **_kwargs: _credentials())
    monkeypatch.setattr(
        auto,
        "recovery_capabilities",
        lambda origin: {
            "origin": origin,
            "recurring_receipt_lookup_url": (
                f"{origin}/api/recurring-receipts/{{client_submission_id}}"
            ),
        },
    )
    receipt_calls: list[str] = []

    def receipt(*_args, **_kwargs):
        assert _kwargs["token"] == "recovery-secret"
        receipt_calls.append("lookup")
        return {
            "receipt_id": "receipt-only",
            "accepted_at": "2026-07-15T13:00:00+00:00",
            "status": "accepted",
        }

    monkeypatch.setattr(auto, "lookup_receipt", receipt)
    forbidden_calls: list[str] = []

    def forbidden(*_args, **_kwargs):
        forbidden_calls.append("forbidden")
        raise AssertionError("receipt-only recovery must not scan, run AI, or POST")

    monkeypatch.setattr(auto, "fetch_capabilities", forbidden)
    monkeypatch.setattr(auto, "get_enrollment", forbidden)
    monkeypatch.setattr(auto, "submit_artifact", forbidden)
    monkeypatch.setattr(auto, "package", forbidden)

    result = auto.run_cycle(force=False, scheduled_client="claude")

    assert result["code"] == "uploaded"
    assert result["receipt_reference"] == "receipt-only"
    assert receipt_calls == ["lookup"]
    assert forbidden_calls == []
    conn = open_index()
    try:
        row = conn.execute(
            "SELECT submission_state, hosted_receipt_id FROM shares WHERE share_id = ?",
            (share_id,),
        ).fetchone()
        assert tuple(row) == ("accepted", "receipt-only")
        recovered = get_auto_upload_enrollment(conn)
        assert recovered["health"] == "action_required"
        assert recovered["last_completed_at"] is not None
        assert recovered["last_receipt_reference"] == "receipt-only"
    finally:
        conn.close()


def test_receipt_commit_atomically_checkpoints_cadence_before_restart(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(
        conn,
        config,
        enrolled_at="2026-07-01T00:00:00+00:00",
    )
    share_id, _ = _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="submitting",
    )

    receipt_id = auto._commit_receipt(
        conn,
        share_id=share_id,
        receipt={
            "receipt_id": "receipt-before-process-death",
            "accepted_at": "2026-07-15T13:00:00+00:00",
            "status": "accepted",
        },
    )

    assert receipt_id == "receipt-before-process-death"
    assert tuple(
        conn.execute(
            "SELECT submission_state, hosted_receipt_id FROM shares WHERE share_id = ?",
            (share_id,),
        ).fetchone()
    ) == ("accepted", "receipt-before-process-death")
    checkpoint = get_auto_upload_enrollment(conn)
    assert checkpoint["last_completed_at"] is not None
    assert checkpoint["last_result_count"] == 1
    assert checkpoint["last_receipt_reference"] == "receipt-before-process-death"
    assert auto.due_decision(checkpoint).due is False
    conn.close()

    forbidden_calls: list[str] = []

    def forbidden(*_args, **_kwargs):
        forbidden_calls.append("work")
        raise AssertionError("restart after receipt commit must honor the cadence checkpoint")

    monkeypatch.setattr(auto, "load_credentials", forbidden)
    monkeypatch.setattr(auto, "fetch_capabilities", forbidden)
    monkeypatch.setattr(auto, "submit_artifact", forbidden)

    restarted = auto.run_cycle(force=False)

    assert restarted["code"] == "not-due"
    assert forbidden_calls == []


def test_paused_submitting_ledger_remains_visible_and_recovers_before_mode_gate(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config)
    share_id, _ = _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="submitting",
    )
    assert update_auto_upload_enrollment(
        conn,
        expected_generation=1,
        mode="paused",
        generation=2,
        last_result_code="paused",
    )
    conn.close()
    write_credentials(_credentials())
    before = auto.status()
    assert before["mode"] == "paused"
    assert before["pending_submission_state"] == "submitting"
    assert auto.hook_due_check("claude", datetime.now(timezone.utc)).due is True

    monkeypatch.setattr(auto, "recovery_capabilities", lambda origin: _capabilities(origin))
    monkeypatch.setattr(
        auto,
        "lookup_receipt",
        lambda *_args, **_kwargs: {
            "receipt_id": "receipt-while-paused",
            "accepted_at": "2026-07-15T13:30:00+00:00",
            "status": "accepted",
        },
    )
    result = auto.run_cycle(force=False, scheduled_client="claude")

    assert result["code"] == "uploaded"
    conn = open_index()
    try:
        recovered = get_auto_upload_enrollment(conn)
        assert recovered["mode"] == "paused"
        assert recovered["last_completed_at"] is not None
        assert conn.execute(
            "SELECT submission_state FROM shares WHERE share_id = ?", (share_id,)
        ).fetchone()[0] == "accepted"
    finally:
        conn.close()
    assert auto.status()["pending_submission_state"] is None


def test_action_required_receipt_404_never_turns_into_a_second_post(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config, health="action_required")
    share_id, _ = _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="submitting",
    )
    conn.close()
    monkeypatch.setattr(auto, "load_credentials", lambda **_kwargs: _credentials())
    monkeypatch.setattr(
        auto,
        "recovery_capabilities",
        lambda origin: {
            "origin": origin,
            "recurring_receipt_lookup_url": (
                f"{origin}/api/recurring-receipts/{{client_submission_id}}"
            ),
        },
    )
    monkeypatch.setattr(
        auto,
        "lookup_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RecurringServiceError("receipt_not_found", "not found", status=404)
        ),
    )
    forbidden_calls: list[str] = []

    def forbidden(*_args, **_kwargs):
        forbidden_calls.append("forbidden")
        raise AssertionError("receipt 404 under action_required must not retry POST")

    monkeypatch.setattr(auto, "fetch_capabilities", forbidden)
    monkeypatch.setattr(auto, "get_enrollment", forbidden)
    monkeypatch.setattr(auto, "submit_artifact", forbidden)
    monkeypatch.setattr(auto, "package", forbidden)

    result = auto.run_cycle(force=True)

    assert result["code"] == "submission_requires_fresh_gates"
    assert forbidden_calls == []
    conn = open_index()
    try:
        assert conn.execute(
            "SELECT submission_state FROM shares WHERE share_id = ?", (share_id,)
        ).fetchone()[0] == "sealed"
        assert get_auto_upload_enrollment(conn)["health"] == "action_required"
    finally:
        conn.close()


def test_definite_receipt_404_unblocks_explicit_reauthorization(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config, health="action_required")
    share_id, artifact_path = _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="submitting",
    )
    conn.close()
    write_credentials(_credentials())
    monkeypatch.setattr(
        auto,
        "recovery_capabilities",
        lambda origin: {
            "origin": origin,
            "recurring_receipt_lookup_url": (
                f"{origin}/api/recurring-receipts/{{client_submission_id}}"
            ),
        },
    )
    monkeypatch.setattr(
        auto,
        "lookup_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RecurringServiceError("receipt_not_found", "not found", status=404)
        ),
    )

    recovery = auto.run_cycle(force=True)

    assert recovery["code"] == "submission_requires_fresh_gates"
    conn = open_index()
    try:
        assert conn.execute(
            "SELECT submission_state FROM shares WHERE share_id = ?", (share_id,)
        ).fetchone()[0] == "sealed"
    finally:
        conn.close()

    _patch_enable_dependencies(monkeypatch)
    monkeypatch.setattr(
        auto,
        "update_enrollment",
        lambda *_args, **_kwargs: {
            "enrollment_id": "server-enrollment-1",
            "enrolled_at": ENROLLED_AT,
            "authorization_revision": 2,
        },
    )

    enabled = auto.enable(
        agent="claude",
        accepted_authorization_version=AUTH_VERSION,
        accepted_retention_version=RETENTION_VERSION,
        accepted_authorization_profile_hash=_current_authorization_profile_hash(),
    )

    assert enabled["mode"] == "enabled"
    assert enabled["generation"] == 2
    assert not artifact_path.exists()
    conn = open_index()
    try:
        assert conn.execute(
            "SELECT 1 FROM shares WHERE share_id = ?", (share_id,)
        ).fetchone() is None
        assert get_auto_upload_enrollment(conn)["authorization_revision"] == 2
    finally:
        conn.close()


def test_config_profile_mutation_before_final_transition_stops_post(
    isolated_auto_upload,
    monkeypatch,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    enrollment = _save_enabled_enrollment(conn, config)
    share_id, _ = _create_pending_share(
        conn,
        isolated_auto_upload["install"],
        session_id="session-one",
        enrollment_id="server-enrollment-1",
        state="sealed",
    )
    monkeypatch.setattr(auto, "fetch_capabilities", lambda **_kwargs: _capabilities())
    remote_calls: list[str] = []

    def mutate_profile_at_remote_gate(*_args, **_kwargs):
        remote_calls.append("remote-gate")
        changed = config_module.load_config()
        changed["redact_strings"] = ["late-profile-change"]
        config_module.save_config(changed)
        return {
            "enrollment_id": "server-enrollment-1",
            "submissions_open": True,
            "terms_current": True,
            "authorization_revision": 1,
            "authorization_version": AUTH_VERSION,
            "retention_policy_version": RETENTION_VERSION,
            "scope_hash": auto.scope_hash(["claude"], ["project-one"]),
            "revoked_at": None,
        }

    monkeypatch.setattr(auto, "get_enrollment", mutate_profile_at_remote_gate)
    post_calls: list[str] = []
    monkeypatch.setattr(
        auto,
        "submit_artifact",
        lambda *_args, **_kwargs: post_calls.append("POST"),
    )

    with pytest.raises(auto.ControlChanged):
        auto._reconcile_pending(
            conn,
            enrollment=enrollment,
            credentials=_credentials(),
            capabilities=_capabilities(),
            allow_submit=True,
        )

    assert remote_calls == ["remote-gate"]
    assert post_calls == []
    row = conn.execute(
        "SELECT submission_state FROM shares WHERE share_id = ?", (share_id,)
    ).fetchone()
    assert row is None
    changed_enrollment = get_auto_upload_enrollment(conn)
    assert changed_enrollment["mode"] == "paused"
    assert changed_enrollment["health"] == "action_required"
    assert changed_enrollment["generation"] == enrollment["generation"] + 1
    assert changed_enrollment["last_result_code"] == "profile_changed"
    conn.close()


def test_policy_and_allowlist_mutations_pause_and_advance_generation(
    isolated_auto_upload,
):
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    enrollment = _save_enabled_enrollment(conn, config)
    expected_generation = enrollment["generation"]

    policy_id = add_policy(conn, "redact_string", "private-value", reason="test")
    expected_generation += 1
    after_add = get_auto_upload_enrollment(conn)
    assert after_add["mode"] == "paused"
    assert after_add["health"] == "action_required"
    assert after_add["generation"] == expected_generation

    assert remove_policy(conn, policy_id) is True
    expected_generation += 1
    after_remove = get_auto_upload_enrollment(conn)
    assert after_remove["mode"] == "paused"
    assert after_remove["generation"] == expected_generation

    entry, _, _ = allowlist_add_by_hash(
        conn,
        entity_hash="a" * 64,
        entity_type="email",
        entity_label="test entry",
        reason="test",
        added_by="test",
    )
    conn.commit()
    expected_generation += 1
    after_allowlist_add = get_auto_upload_enrollment(conn)
    assert after_allowlist_add["mode"] == "paused"
    assert after_allowlist_add["generation"] == expected_generation

    removed, _, _ = allowlist_remove(conn, entry["allowlist_id"])
    conn.commit()
    assert removed is True
    expected_generation += 1
    after_allowlist_remove = get_auto_upload_enrollment(conn)
    assert after_allowlist_remove["mode"] == "paused"
    assert after_allowlist_remove["health"] == "action_required"
    assert after_allowlist_remove["generation"] == expected_generation
    assert after_allowlist_remove["last_result_code"] == "profile_changed"
    conn.close()



def test_control_changed_during_cycle_records_backoff(isolated_auto_upload, monkeypatch):
    # A control change mid-cycle must record exponential backoff, not leave the
    # enrollment health='ready' with no next_retry_at (which would relaunch a
    # full cycle on every SessionStart for a persistent mismatch).
    config = _save_scope_config()
    conn = open_index()
    _seed_released_session(conn, isolated_auto_upload["root"])
    _save_enabled_enrollment(conn, config, enrolled_at="2026-07-01T00:00:00+00:00")
    conn.close()
    monkeypatch.setattr(auto, "load_credentials", lambda **_kwargs: _credentials())
    _patch_runner_host(monkeypatch)
    _patch_strict_scanner(monkeypatch)

    def control_changed(**_kwargs):
        raise auto.ControlChanged("scope drifted mid-cycle")

    monkeypatch.setattr(auto, "_assert_control_state", control_changed)

    result = auto.run_cycle(force=True)

    assert result["code"] == "control_changed"
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["health"] == "retrying"
        assert enrollment["consecutive_failures"] == 1
        assert enrollment["next_retry_at"] is not None
    finally:
        conn.close()
