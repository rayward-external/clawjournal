"""Tests for the centralized release gate and bundle manifest redactions section."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from clawjournal.findings import (
    hash_entity,
    reset_salt_cache,
    write_findings_to_db,
)
from clawjournal.redaction.pii import scan_session_for_pii_findings
from clawjournal.redaction.secrets import scan_session_for_findings
from clawjournal.workbench.index import (
    apply_share_redactions,
    build_session_redactions_summary,
    create_share,
    export_share_to_disk,
    get_share,
    open_index,
    release_gate_blockers,
    set_hold_state,
    upsert_sessions,
)


_FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkphbmUifQ"
    ".abcdefghijABCDEFGH0123456789"
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path)
    reset_salt_cache()
    connection = open_index()
    yield connection
    connection.close()
    reset_salt_cache()


def _settled_session(session_id, content=None):
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    return {
        "session_id": session_id,
        "project": "demo",
        "source": "claude",
        "model": "claude-sonnet-4",
        "start_time": old,
        "end_time": old,
        "git_branch": "main",
        "display_title": "t",
        "messages": [
            {"role": "user", "content": content or f"secret: {_FAKE_JWT}",
             "thinking": "", "tool_uses": []},
        ],
        "stats": {"user_messages": 1, "assistant_messages": 0, "tool_uses": 0,
                  "input_tokens": 1, "output_tokens": 0},
    }


class TestReleaseGate:
    def test_default_auto_redacted_is_shareable(self, conn):
        upsert_sessions(conn, [_settled_session("a"), _settled_session("b")])
        conn.commit()
        assert release_gate_blockers(conn, ["a", "b"]) == []

    def test_pending_review_blocks(self, conn):
        upsert_sessions(conn, [_settled_session("a")])
        conn.commit()
        set_hold_state(conn, "a", "pending_review", changed_by="user")
        blockers = release_gate_blockers(conn, ["a"])
        assert len(blockers) == 1
        assert blockers[0]["hold_state"] == "pending_review"

    def test_released_passes(self, conn):
        upsert_sessions(conn, [_settled_session("a")])
        conn.commit()
        set_hold_state(conn, "a", "released", changed_by="user")
        assert release_gate_blockers(conn, ["a"]) == []

    def test_past_due_embargo_treated_as_released(self, conn):
        upsert_sessions(conn, [_settled_session("a")])
        conn.commit()
        future = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        set_hold_state(conn, "a", "embargoed", changed_by="user", embargo_until=future)
        # Freeze `now` in the future → embargo has expired.
        later = datetime.now(timezone.utc) + timedelta(days=1)
        assert release_gate_blockers(conn, ["a"], now=later) == []

    def test_future_embargo_blocks(self, conn):
        upsert_sessions(conn, [_settled_session("a")])
        conn.commit()
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        set_hold_state(conn, "a", "embargoed", changed_by="user", embargo_until=future)
        blockers = release_gate_blockers(conn, ["a"])
        assert len(blockers) == 1
        assert blockers[0]["hold_state"] == "embargoed"
        assert blockers[0]["embargo_until"] == future

    def test_missing_session_is_blocker(self, conn):
        blockers = release_gate_blockers(conn, ["does-not-exist"])
        assert blockers == [{"session_id": "does-not-exist", "hold_state": "missing"}]

    def test_empty_ids_no_blockers(self, conn):
        assert release_gate_blockers(conn, []) == []


class TestRedactionsSummary:
    def test_applied_vs_ignored(self, conn):
        # Two distinct entities so one can be ignored independently.
        content = (
            f"jwt: {_FAKE_JWT}\n"
            "github: ghp_abcdefghijklmnopqrstuvwxyzABCDEF0123"
        )
        sess = _settled_session("sess-1", content=content)
        upsert_sessions(conn, [sess])
        raw = scan_session_for_findings(sess)
        assert raw, "fixture should produce raw findings"
        write_findings_to_db(conn, "sess-1", raw, revision="v1:r")
        conn.execute(
            "UPDATE sessions SET findings_revision='v1:r' WHERE session_id='sess-1'"
        )
        conn.commit()

        # Ignore the JWT finding by rule; leave github_token as applied.
        conn.execute(
            "UPDATE findings SET status='ignored', decided_by='user' "
            "WHERE rule = 'jwt'"
        )
        conn.commit()

        summary = build_session_redactions_summary(conn, "sess-1")
        assert summary["findings_revision"] == "v1:r"
        # Has at least one entry in each bucket after the split.
        assert summary["applied"]
        assert summary["ignored"]
        for entry in summary["applied"]:
            assert "count" in entry
            assert entry["engine"] == "regex_secrets"
        via_values = {entry.get("via") for entry in summary["ignored"]}
        assert "user" in via_values

    def test_allowlist_origin_tagged(self, conn):
        sess = _settled_session("sess-1")
        upsert_sessions(conn, [sess])
        raw = scan_session_for_findings(sess)
        write_findings_to_db(conn, "sess-1", raw, revision="v1:r")
        conn.commit()
        conn.execute(
            "UPDATE findings SET status='ignored', decided_by='allowlist', "
            "       decision_source_id = 'fake-aid'"
        )
        conn.commit()
        summary = build_session_redactions_summary(conn, "sess-1")
        assert all(entry.get("via") == "allowlist" for entry in summary["ignored"])
        assert summary["applied"] == []

    def test_no_findings_rows_yields_empty_buckets(self, conn):
        upsert_sessions(conn, [_settled_session("clean", content="no secrets here")])
        conn.commit()
        summary = build_session_redactions_summary(conn, "clean")
        assert summary == {
            "findings_revision": None,
            "applied": [],
            "ignored": [],
        }


class TestExportManifestRedactions:
    def test_manifest_carries_per_session_redactions(self, conn, tmp_path):
        sess = _settled_session("sess-1")
        upsert_sessions(conn, [sess])
        raw = scan_session_for_findings(sess)
        write_findings_to_db(conn, "sess-1", raw, revision="v1:test")
        conn.execute(
            "UPDATE sessions SET findings_revision='v1:test' WHERE session_id='sess-1'"
        )
        conn.commit()

        share_id = create_share(conn, ["sess-1"], note="t")
        share = get_share(conn, share_id)
        # Omit output_path so export lands under the monkeypatched
        # CONFIG_DIR (tmp_path) — the explicit-output-path validator
        # in export_share_to_disk rejects macOS tmpdirs that are
        # neither under HOME nor literal /tmp.
        export_dir, manifest = export_share_to_disk(
            conn, share_id, share,
        )
        assert export_dir is not None
        assert len(manifest["sessions"]) == 1
        entry = manifest["sessions"][0]
        assert entry["session_id"] == "sess-1"
        assert "redactions" in entry
        assert entry["redactions"]["findings_revision"] == "v1:test"
        # Aggregated counts only — no hashes, offsets, or plaintext.
        dumped = json.dumps(entry["redactions"])
        assert _FAKE_JWT not in dumped
        assert "entity_hash" not in entry["redactions"]

    def test_apply_findings_to_blob_covers_both_engines(self, conn):
        # JWT + email in the same session — `apply_findings_to_blob` is
        # the DB-backed deterministic apply path used by share-time
        # redaction. Locking the two-engine contract here keeps the
        # blob-level replace coherent regardless of who invokes it.
        from clawjournal.redaction.secrets import apply_findings_to_blob
        EMAIL = "alice@example.com"
        sess = _settled_session(
            "sess-mix", content=f"jwt={_FAKE_JWT} contact {EMAIL}"
        )
        upsert_sessions(conn, [sess])
        raw = scan_session_for_findings(sess) + scan_session_for_pii_findings(sess)
        write_findings_to_db(conn, "sess-mix", raw, revision="v1:mix")
        conn.commit()
        redacted, n = apply_findings_to_blob(sess, conn, "sess-mix")
        body = redacted["messages"][0]["content"]
        assert _FAKE_JWT not in body
        assert EMAIL not in body
        assert "[REDACTED_EMAIL]" in body
        assert "[REDACTED_JWT]" in body
        assert n >= 2

    def test_apply_share_redactions_uses_trufflehog_in_data_step(self, conn, monkeypatch):
        from clawjournal.redaction import trufflehog as trufflehog_scanner

        raw = "xoxb-0123456789-ABCDEFGHIJKL"
        sess = _settled_session("sess-th", content=f"leak {raw}")
        upsert_sessions(conn, [sess])
        conn.commit()

        monkeypatch.delenv(trufflehog_scanner.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(trufflehog_scanner, "is_available", lambda: True)
        monkeypatch.setattr(
            trufflehog_scanner,
            "_scan_text_for_raw_matches",
            lambda text: [{"raw": raw, "detector": "Slack", "status": "verified"}],
        )

        redacted, n, log = apply_share_redactions(conn, sess)
        body = redacted["messages"][0]["content"]
        assert raw not in body
        assert "[REDACTED_SLACK]" in body
        assert n >= 1
        assert any(entry["type"] == "trufflehog_slack" for entry in log)

    def test_apply_share_redactions_raises_on_missing_session_id(self, conn):
        """Deterministic engines route through the findings table,
        keyed by session_id. A session stripped of its ID would
        silently ship un-redacted; fail loud instead."""
        session = {
            "session_id": "",
            "project": "demo",
            "messages": [{"role": "user", "content": "leak", "tool_uses": []}],
        }
        with pytest.raises(ValueError, match="session_id"):
            apply_share_redactions(conn, session)

    def test_apply_share_redactions_honors_ignored_trufflehog_findings(self, conn, monkeypatch):
        from clawjournal.redaction import trufflehog as trufflehog_scanner

        raw = "xoxb-ignored-at-share-time-ABCDEFGHIJKL"
        sess = _settled_session("sess-th-ignore", content=f"keep {raw}")
        upsert_sessions(conn, [sess])
        conn.execute(
            "INSERT INTO findings "
            "(finding_id, session_id, engine, rule, entity_type, entity_hash, "
            " entity_length, field, message_index, tool_field, offset, length, "
            " confidence, status, decided_by, decision_source_id, decided_at, "
            " decision_reason, revision, created_at) "
            "VALUES ('fid-th', 'sess-th-ignore', 'trufflehog', 'Slack', 'Slack', ?, ?, "
            "        'content', 0, NULL, 0, ?, 1.0, 'ignored', 'user', NULL, "
            "        '2026-04-20T00:00:00', NULL, 'v1:test', '2026-04-20T00:00:00')",
            (hash_entity(raw), len(raw), len(raw)),
        )
        conn.commit()

        monkeypatch.delenv(trufflehog_scanner.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(trufflehog_scanner, "is_available", lambda: True)
        monkeypatch.setattr(
            trufflehog_scanner,
            "_scan_text_for_raw_matches",
            lambda text: [{"raw": raw, "detector": "Slack", "status": "verified"}],
        )

        redacted, n, log = apply_share_redactions(conn, sess)
        body = redacted["messages"][0]["content"]
        assert raw in body
        assert "[REDACTED_SLACK]" not in body
        assert n == 0
        assert not any(entry["type"] == "trufflehog_slack" for entry in log)


class TestAiTextRedaction:
    """The judge writes free-text fields (reasoning, summary, evidence
    paraphrases, learning summary) into ``ai_scoring_detail`` and the
    top-level ``ai_learning_summary`` column. These fields are exported
    in the share bundle, so share-time redaction has to walk them with
    the same engines that handle ``display_title`` / message content.

    These tests pin the exact field names used by the rubric — they
    caught a "reason" vs "reasoning" typo where the constants did not
    match what the judge actually writes (see
    ``scoring.score_session``'s detail_data construction)."""

    def _session_with_judge_text(self, session_id, *, custom_marker="LAB-PROJECT-X"):
        sess = _settled_session(session_id, content="harmless")
        # Mirror the actual detail_data shape from scoring.score_session.
        sess["ai_learning_summary"] = f"Top-level note about {custom_marker}."
        sess["ai_scoring_detail"] = json.dumps({
            "substance": 4,
            "resolution": "resolved",
            "reasoning": f"Judge cited {custom_marker} in its reasoning.",
            "display_title": f"Fix {custom_marker} pipeline",
            "summary": f"Walked through the {custom_marker} dataset and fixed it.",
            "task_type": "data_analysis",
            "session_tags": ["statistics"],
            "privacy_flags": [],
            "project_areas": [f"/Users/student/{custom_marker}/notebooks/"],
            "ai_failure_value_score": 4,
            "ai_recovery_labels": ["user_corrected_recovery"],
            "ai_failure_attribution": "agent_caused",
            "ai_failure_modes": ["reasoning_fabrication"],
            "ai_failure_evidence": [
                f"User: 'the {custom_marker} samples are paired'.",
                f"Agent then revised the {custom_marker} test.",
            ],
            "ai_learning_summary": f"Embedded summary mentions {custom_marker} too.",
        })
        return sess

    def test_apply_to_ai_text_walks_reasoning_field(self):
        """Regression: the constants must reference the actual key written
        by the judge. ``ai_scoring_detail.reasoning`` (not ``reason``) must
        appear in the visited-label list."""
        from clawjournal.workbench.index import _apply_to_ai_text

        sess = self._session_with_judge_text("apply-1")
        visited: list[str] = []
        _apply_to_ai_text(sess, lambda text, label: (visited.append(label), text)[1])

        assert "ai_learning_summary" in visited
        assert "ai_scoring_detail.reasoning" in visited
        assert "ai_scoring_detail.display_title" in visited
        assert "ai_scoring_detail.summary" in visited
        assert "ai_scoring_detail.ai_learning_summary" in visited
        assert "ai_scoring_detail.ai_failure_evidence[0]" in visited
        assert "ai_scoring_detail.ai_failure_evidence[1]" in visited
        assert "ai_scoring_detail.project_areas[0]" in visited
        # Sanity: at minimum the 8 fields we know are free-text in the
        # actual rubric. Enum/numeric fields must not appear.
        assert len(visited) >= 8
        assert "ai_scoring_detail.task_type" not in visited
        assert "ai_scoring_detail.session_tags" not in visited

    def test_share_redactions_redact_custom_strings_from_reasoning(self, conn):
        """End-to-end: a custom string visible only in ``detail.reasoning``
        must be redacted by the custom-strings engine at share time. If
        the constants typo regresses to ``reason``, this fails."""
        marker = "FOO-LAB-PROJ"
        sess = self._session_with_judge_text("redact-1", custom_marker=marker)
        upsert_sessions(conn, [sess])
        conn.commit()
        redacted, _, _ = apply_share_redactions(
            conn, sess, custom_strings=[marker],
        )
        detail = json.loads(redacted["ai_scoring_detail"])
        assert marker not in detail["reasoning"]
        assert marker not in detail["display_title"]
        assert marker not in detail["summary"]
        assert marker not in detail["ai_learning_summary"]
        assert marker not in json.dumps(detail["ai_failure_evidence"])
        assert marker not in json.dumps(detail["project_areas"])
        assert marker not in redacted["ai_learning_summary"]
        # Enum fields untouched.
        assert detail["task_type"] == "data_analysis"
        assert detail["ai_failure_attribution"] == "agent_caused"

    def test_findings_to_blob_redacts_secret_paraphrased_into_reasoning(self, conn):
        """Apply path: a secret detected in messages is replaced when it
        appears (paraphrased) in ``detail.reasoning`` too."""
        from clawjournal.redaction.secrets import apply_findings_to_blob

        secret = "AKIA0123456789ABCDEF"
        sess = _settled_session("findings-1", content=f"key={secret}")
        sess["ai_scoring_detail"] = json.dumps({
            "reasoning": f"Agent verified the key {secret} works.",
            "ai_failure_evidence": [f"User shared {secret} in chat."],
        })
        upsert_sessions(conn, [sess])
        raw = scan_session_for_findings(sess)
        write_findings_to_db(conn, "findings-1", raw, revision="v1:reasoning")
        conn.commit()

        redacted, n = apply_findings_to_blob(sess, conn, "findings-1")
        detail = json.loads(redacted["ai_scoring_detail"])
        assert secret not in detail["reasoning"]
        assert secret not in detail["ai_failure_evidence"][0]
        assert "[REDACTED_AWS_KEY]" in detail["reasoning"]
        assert "[REDACTED_AWS_KEY]" in detail["ai_failure_evidence"][0]
        # message-level redaction still happens
        assert secret not in redacted["messages"][0]["content"]


class TestTruffleHogGate:
    """The post-redaction TruffleHog scan is mandatory on every share
    export. These tests disable the test-suite-wide bypass and mock
    the scan to cover the three gate outcomes (clean / findings /
    missing binary)."""

    def _share(self, conn):
        sess = _settled_session("sess-1")
        upsert_sessions(conn, [sess])
        raw = scan_session_for_findings(sess)
        write_findings_to_db(conn, "sess-1", raw, revision="v1:t")
        conn.execute(
            "UPDATE sessions SET findings_revision='v1:t' WHERE session_id='sess-1'"
        )
        conn.commit()
        share_id = create_share(conn, ["sess-1"], note="t")
        return share_id, get_share(conn, share_id)

    def test_clean_scan_advances_share_status(self, conn, monkeypatch):
        from clawjournal.redaction import trufflehog as trufflehog_scanner
        monkeypatch.delenv(trufflehog_scanner.SKIP_ENV_VAR, raising=False)

        def fake_scan(path):
            return trufflehog_scanner.TruffleHogReport(
                scanned_path=str(path), scanned_sha256="sha256:0",
            )

        monkeypatch.setattr(trufflehog_scanner, "scan_file", fake_scan)
        share_id, share = self._share(conn)
        export_dir, manifest = export_share_to_disk(conn, share_id, share)
        assert export_dir is not None
        assert manifest.get("blocked") is not True
        status_row = conn.execute(
            "SELECT status FROM shares WHERE share_id = ?", (share_id,)
        ).fetchone()
        assert status_row["status"] in ("shared", "exported")
        th = manifest["redaction_summary"]["trufflehog"]
        assert th["findings"] == 0
        assert (export_dir / "trufflehog.json").exists()

    def test_findings_block_and_do_not_advance_status(self, conn, monkeypatch):
        from clawjournal.redaction import trufflehog as trufflehog_scanner
        monkeypatch.delenv(trufflehog_scanner.SKIP_ENV_VAR, raising=False)

        def fake_scan(path):
            return trufflehog_scanner.TruffleHogReport(
                scanned_path=str(path),
                scanned_sha256="sha256:0",
                findings=[
                    trufflehog_scanner.TruffleHogFinding(
                        detector="GitHub", status="verified",
                        line=3, masked="ghp_a***4567",
                        raw_sha256="sha256:x",
                    )
                ],
                verified=1,
                top_detectors=["GitHub"],
            )

        monkeypatch.setattr(trufflehog_scanner, "scan_file", fake_scan)
        share_id, share = self._share(conn)
        pre_status_row = conn.execute(
            "SELECT status FROM shares WHERE share_id = ?", (share_id,)
        ).fetchone()
        pre_status = pre_status_row["status"]

        export_dir, manifest = export_share_to_disk(conn, share_id, share)
        assert export_dir is not None
        assert manifest["blocked"] is True
        assert manifest["block_reason"] == "trufflehog-findings"
        assert "ghp_a***4567" in manifest["block_message"]
        assert manifest.get("blocked_sessions") is None
        # Status must not advance — the share is not clean.
        post_status_row = conn.execute(
            "SELECT status FROM shares WHERE share_id = ?", (share_id,)
        ).fetchone()
        assert post_status_row["status"] == pre_status
        # Export dir preserved for debugging; report on disk.
        assert (export_dir / "sessions.jsonl").exists()
        assert (export_dir / "trufflehog.json").exists()
        # Manifest-on-disk matches the returned manifest (blocked=true).
        disk = json.loads((export_dir / "manifest.json").read_text())
        assert disk["blocked"] is True

    def test_findings_block_maps_jsonl_line_to_session(self, conn, monkeypatch):
        from clawjournal.redaction import trufflehog as trufflehog_scanner
        monkeypatch.delenv(trufflehog_scanner.SKIP_ENV_VAR, raising=False)

        def fake_scan(path):
            return trufflehog_scanner.TruffleHogReport(
                scanned_path=str(path),
                scanned_sha256="sha256:0",
                findings=[
                    trufflehog_scanner.TruffleHogFinding(
                        detector="NpmToken", status="unverified",
                        line=1, masked="407e***c7fa",
                        raw_sha256="sha256:x",
                    )
                ],
                unverified=1,
                top_detectors=["NpmToken"],
            )

        monkeypatch.setattr(trufflehog_scanner, "scan_file", fake_scan)
        share_id, share = self._share(conn)

        export_dir, manifest = export_share_to_disk(conn, share_id, share)

        assert export_dir is not None
        blocked = manifest["blocked_sessions"]
        assert blocked == [{
            "session_id": "sess-1",
            "project": "demo",
            "source": "claude",
            "model": "claude-sonnet-4",
            "line": 1,
            "findings": [{
                "line": 1,
                "detector": "NpmToken",
                "status": "unverified",
                "masked": "407e***c7fa",
            }],
        }]

    def test_missing_binary_blocks_with_install_hint(self, conn, monkeypatch):
        from clawjournal.redaction import trufflehog as trufflehog_scanner
        monkeypatch.delenv(trufflehog_scanner.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(trufflehog_scanner, "is_available", lambda: False)

        share_id, share = self._share(conn)
        export_dir, manifest = export_share_to_disk(conn, share_id, share)
        assert manifest["blocked"] is True
        assert manifest["block_reason"] == "trufflehog-not-installed"
        assert "brew install trufflehog" in manifest["block_message"]

    def test_scan_error_blocks_with_deterministic_reason(self, conn, monkeypatch):
        from clawjournal.redaction import trufflehog as trufflehog_scanner
        monkeypatch.delenv(trufflehog_scanner.SKIP_ENV_VAR, raising=False)

        def fake_scan(path):
            return trufflehog_scanner.TruffleHogReport(
                scanned_path=str(path),
                scanned_sha256="sha256:0",
                scan_error="unexpected exit status 2",
            )

        monkeypatch.setattr(trufflehog_scanner, "scan_file", fake_scan)
        share_id, share = self._share(conn)
        pre_status = conn.execute(
            "SELECT status FROM shares WHERE share_id = ?",
            (share_id,),
        ).fetchone()["status"]

        export_dir, manifest = export_share_to_disk(conn, share_id, share)
        assert export_dir is not None
        assert manifest["blocked"] is True
        assert manifest["block_reason"] == "trufflehog-error"
        assert "unexpected exit status 2" in manifest["block_message"]
        assert manifest["redaction_summary"]["trufflehog"]["scan_error"] == "unexpected exit status 2"
        post_status = conn.execute(
            "SELECT status FROM shares WHERE share_id = ?",
            (share_id,),
        ).fetchone()["status"]
        assert post_status == pre_status
        assert (export_dir / "trufflehog.json").exists()

    def test_bypass_env_var_recorded_in_manifest(self, conn):
        # Autouse fixture sets the bypass var — verify the manifest
        # records the bypass so reviewers can tell scanned shares from
        # bypassed ones.
        share_id, share = self._share(conn)
        export_dir, manifest = export_share_to_disk(conn, share_id, share)
        assert manifest.get("blocked") is not True
        th = manifest["redaction_summary"]["trufflehog"]
        assert th["bypassed"] is True
