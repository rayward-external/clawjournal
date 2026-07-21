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

    def test_apply_share_redactions_strips_terminal_control_sequences(self, conn):
        """Terminal ANSI escape codes are stripped from shared content so they
        don't bloat the bundle or trip TruffleHog's broad detectors."""
        sess = _settled_session("sess-ansi", content="\x1b[90mnull\x1b[0m done")
        sess["messages"][0]["thinking"] = "think \x1b[31mred\x1b[0m"
        sess["messages"][0]["tool_uses"] = [
            {"name": "bash", "input": "ls \x1b[1mx\x1b[0m", "output": "ok\x1b[0m\x07"}
        ]
        upsert_sessions(conn, [sess])
        conn.commit()

        redacted, _, _ = apply_share_redactions(conn, sess)
        msg = redacted["messages"][0]
        assert msg["content"] == "null done"
        assert "\x1b" not in msg["thinking"]
        assert "\x1b" not in msg["tool_uses"][0]["input"]
        assert "\x1b" not in msg["tool_uses"][0]["output"]

    def test_apply_share_redactions_strips_ansi_in_widened_message_fields(self, conn):
        """ANSI must also be stripped from the widened message model
        (author / invocations / snippets / extra), not just legacy fields."""
        sess = _settled_session("sess-widened", content="hi")
        msg = sess["messages"][0]
        msg["author"] = "agent\x1b[0m"
        msg["invocations"] = [{"name": "bash", "result": "out \x1b[31mred\x1b[0m"}]
        msg["snippets"] = [{"code": "x = 1 \x1b[2K"}]
        msg["extra"] = {"raw": {"line": "deep \x1b[90mgray\x1b[0m"}}
        upsert_sessions(conn, [sess])
        conn.commit()

        redacted, _, _ = apply_share_redactions(conn, sess)
        m = redacted["messages"][0]
        assert "\x1b" not in m["author"]
        assert "\x1b" not in m["invocations"][0]["result"]
        assert "\x1b" not in m["snippets"][0]["code"]
        assert "\x1b" not in m["extra"]["raw"]["line"]

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


def _mock_gate_engines(monkeypatch, *, bl_passes=None, th_passes=None):
    """Enable a real (non-bypassed) share gate with both engines mocked.

    ``bl_passes`` / ``th_passes`` are per-scan-pass lists of raw-match
    dicts (betterleaks: ``{"raw","rule_id","entropy","line"}``;
    trufflehog: ``{"raw","detector","status","line"}``). Passes beyond
    the end of a list return no matches, which is how a redact loop
    converges. The findings-engine hooks are stubbed empty so the
    apply path never spawns a real subprocess.
    """
    from clawjournal.redaction import betterleaks, trufflehog

    monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
    monkeypatch.delenv(betterleaks.SKIP_ENV_VAR, raising=False)
    monkeypatch.setattr(trufflehog, "is_available", lambda: True)
    monkeypatch.setattr(betterleaks, "is_available", lambda: True)
    monkeypatch.setattr(trufflehog, "_scan_text_for_raw_matches", lambda text: [])
    monkeypatch.setattr(betterleaks, "_scan_text_for_raw_matches", lambda text: [])

    calls = {"bl": 0, "th": 0}

    def fake_bl(path):
        i = calls["bl"]
        calls["bl"] += 1
        raws = bl_passes[i] if bl_passes and i < len(bl_passes) else []
        report = betterleaks.BetterleaksReport(
            scanned_path=str(path), scanned_sha256="sha256:0"
        )
        return report, [dict(r) for r in raws]

    def fake_th(path, *, results="verified,unknown,unverified"):
        # The tiered gate must run TruffleHog verified-only.
        assert results == "verified", f"gate must scan verified-only, got {results!r}"
        i = calls["th"]
        calls["th"] += 1
        raws = th_passes[i] if th_passes and i < len(th_passes) else []
        report = trufflehog.TruffleHogReport(
            scanned_path=str(path), scanned_sha256="sha256:0"
        )
        return report, [dict(r) for r in raws]

    monkeypatch.setattr(betterleaks, "scan_file_with_raws", fake_bl)
    monkeypatch.setattr(trufflehog, "scan_file_with_raws", fake_th)
    return calls


class TestSecretScanGate:
    """The post-redaction secret-scan gate is mandatory on every share
    export. Tier semantics: TruffleHog-verified → block; private-key
    rules → review; recognizable unverified tokens → span-redacted and
    the share proceeds; soft/ignored findings → warn-only."""

    RAW = "tok-A1b2C3d4E5f6G7h8I9j0K1l2M3n4"

    def _share(self, conn, content=None):
        sess = _settled_session("sess-1", content=content)
        upsert_sessions(conn, [sess])
        raw = scan_session_for_findings(sess)
        write_findings_to_db(conn, "sess-1", raw, revision="v1:t")
        conn.execute(
            "UPDATE sessions SET findings_revision='v1:t' WHERE session_id='sess-1'"
        )
        conn.commit()
        share_id = create_share(conn, ["sess-1"], note="t")
        return share_id, get_share(conn, share_id)

    def _status(self, conn, share_id):
        return conn.execute(
            "SELECT status FROM shares WHERE share_id = ?", (share_id,)
        ).fetchone()["status"]

    def test_clean_scan_advances_share_status(self, conn, monkeypatch):
        _mock_gate_engines(monkeypatch)
        share_id, share = self._share(conn)
        export_dir, manifest = export_share_to_disk(conn, share_id, share)
        assert export_dir is not None
        assert manifest.get("blocked") is not True
        assert self._status(conn, share_id) in ("shared", "exported")
        assert manifest["redaction_summary"]["trufflehog"]["findings"] == 0
        scan = manifest["redaction_summary"]["secret_scan"]
        assert scan["findings"] == 0
        assert scan["converged"] is True
        assert (export_dir / "trufflehog.json").exists()
        assert (export_dir / "secret-scan.json").exists()

    def test_unverified_token_is_redacted_and_share_advances(self, conn, monkeypatch):
        # The headline behavior change: a recognizable-but-unverified
        # token no longer rejects the session — the exact span is
        # replaced and the share ships.
        _mock_gate_engines(
            monkeypatch,
            bl_passes=[[{
                "raw": self.RAW, "rule_id": "slack-bot-token",
                "entropy": 4.4, "line": 1,
            }]],
        )
        share_id, share = self._share(conn, content=f"leak {self.RAW} in log")
        export_dir, manifest = export_share_to_disk(conn, share_id, share)

        assert export_dir is not None
        assert manifest.get("blocked") is not True
        assert self._status(conn, share_id) in ("shared", "exported")
        body = (export_dir / "sessions.jsonl").read_text()
        assert self.RAW not in body
        assert "[REDACTED_SLACK_BOT_TOKEN]" in body
        scan = manifest["redaction_summary"]["secret_scan"]
        # The raw can occur more than once in the exported line (e.g.
        # message content plus a derived preview field) — every
        # occurrence is replaced and counted.
        assert scan["gate_redactions"] >= 1
        assert scan["tier_counts"].get("redact") == 1
        assert scan["converged"] is True
        # Folded into the manifest counters and the per-session entry.
        assert (
            manifest["redaction_summary"]["by_type"]["gate_secret_scan"]
            == scan["gate_redactions"]
        )
        assert manifest["sessions"][0]["gate_redactions"] == scan["gate_redactions"]
        # The redacted line is still valid JSON.
        json.loads(body.splitlines()[0])

    def test_verified_credential_blocks_and_does_not_advance(self, conn, monkeypatch):
        _mock_gate_engines(
            monkeypatch,
            th_passes=[[{
                "raw": self.RAW, "detector": "GitHub",
                "status": "verified", "line": 1,
            }]],
        )
        share_id, share = self._share(conn, content=f"leak {self.RAW}")
        pre_status = self._status(conn, share_id)

        export_dir, manifest = export_share_to_disk(conn, share_id, share)
        assert export_dir is not None
        assert manifest["blocked"] is True
        assert manifest["block_reason"] == "secret-scan-findings"
        assert "block" in manifest["block_message"]
        assert self._status(conn, share_id) == pre_status
        blocked = manifest["blocked_sessions"]
        assert blocked[0]["session_id"] == "sess-1"
        finding = blocked[0]["findings"][0]
        assert finding["tier"] == "block"
        assert finding["engine"] == "trufflehog"
        assert finding["detector"] == "GitHub"  # legacy key preserved
        # Defense in depth: even a blocked export's on-disk artifact
        # has the live credential redacted.
        assert self.RAW not in (export_dir / "sessions.jsonl").read_text()
        disk = json.loads((export_dir / "manifest.json").read_text())
        assert disk["blocked"] is True

    def test_private_key_rule_requires_review(self, conn, monkeypatch):
        fake_key = "-----BEGIN RSA PRIVATE KEY-----\\nFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE\\n-----END RSA PRIVATE KEY-----"
        _mock_gate_engines(
            monkeypatch,
            bl_passes=[[{
                "raw": fake_key, "rule_id": "private-key",
                "entropy": 3.9, "line": 1,
            }]],
        )
        share_id, share = self._share(conn, content=f"key: {fake_key}")
        export_dir, manifest = export_share_to_disk(conn, share_id, share)

        assert manifest["blocked"] is True
        assert manifest["block_reason"] == "secret-scan-findings"
        finding = manifest["blocked_sessions"][0]["findings"][0]
        assert finding["tier"] == "review"
        assert finding["rule"] == "private-key"

    def test_ignored_decision_downgrades_to_warn_and_ships(self, conn, monkeypatch):
        # Allowlisting/ignoring a value used to make the redactor skip
        # it and the gate then re-found and blocked it. Now it warns.
        _mock_gate_engines(
            monkeypatch,
            bl_passes=[[{
                "raw": self.RAW, "rule_id": "slack-bot-token",
                "entropy": 4.4, "line": 1,
            }]],
        )
        share_id, share = self._share(conn, content=f"keep {self.RAW}")
        conn.execute(
            "INSERT INTO findings (finding_id, session_id, revision, engine, rule,"
            " entity_type, entity_hash, entity_length, field, message_index,"
            " offset, length, confidence, status, decided_by, created_at)"
            " VALUES ('f-allow', 'sess-1', 'v1:t', 'betterleaks', 'slack-bot-token',"
            " 'slack-bot-token', ?, ?, 'content', 0, 5, ?, 0.9, 'ignored', 'user',"
            " '2026-07-01T00:00:00+00:00')",
            (hash_entity(self.RAW), len(self.RAW), len(self.RAW)),
        )
        conn.commit()

        export_dir, manifest = export_share_to_disk(conn, share_id, share)

        assert manifest.get("blocked") is not True
        assert self._status(conn, share_id) in ("shared", "exported")
        # The ignored value survives in the bundle (user's standing
        # decision) and is recorded as a warning.
        assert self.RAW in (export_dir / "sessions.jsonl").read_text()
        scan = manifest["redaction_summary"]["secret_scan"]
        assert scan["tier_counts"].get("warn") == 1
        assert scan["examples"][0]["tier_reason"] == "decision_ignored"

    def test_allowlisted_but_verified_requires_review(self, conn, monkeypatch):
        # A confirmed-live credential never ships silently, even when
        # the user allowlisted the value — it earns a human checkpoint
        # instead of a hard wall.
        _mock_gate_engines(
            monkeypatch,
            th_passes=[[{
                "raw": self.RAW, "detector": "GitHub",
                "status": "verified", "line": 1,
            }]],
        )
        share_id, share = self._share(conn, content=f"keep {self.RAW}")
        conn.execute(
            "INSERT INTO findings (finding_id, session_id, revision, engine, rule,"
            " entity_type, entity_hash, entity_length, field, message_index,"
            " offset, length, confidence, status, decided_by, created_at)"
            " VALUES ('f-allow', 'sess-1', 'v1:t', 'trufflehog', 'GitHub',"
            " 'GitHub', ?, ?, 'content', 0, 5, ?, 1.0, 'ignored', 'allowlist',"
            " '2026-07-01T00:00:00+00:00')",
            (hash_entity(self.RAW), len(self.RAW), len(self.RAW)),
        )
        conn.commit()

        export_dir, manifest = export_share_to_disk(conn, share_id, share)

        assert manifest["blocked"] is True
        finding = manifest["blocked_sessions"][0]["findings"][0]
        assert finding["tier"] == "review"
        scan = manifest["redaction_summary"]["secret_scan"]
        assert scan["examples"][0]["tier_reason"] == "allowlisted_but_verified"

    def test_soft_rule_warns_and_ships(self, conn, monkeypatch):
        _mock_gate_engines(
            monkeypatch,
            bl_passes=[[{
                "raw": self.RAW, "rule_id": "generic-api-key",
                "entropy": 4.4, "line": 1,
            }]],
        )
        share_id, share = self._share(conn, content=f"cfg {self.RAW}")
        export_dir, manifest = export_share_to_disk(conn, share_id, share)

        assert manifest.get("blocked") is not True
        scan = manifest["redaction_summary"]["secret_scan"]
        assert scan["tier_counts"].get("warn") == 1
        assert scan["examples"][0]["tier_reason"] == "soft_rule"

    def test_non_convergent_redaction_escalates_to_review(self, conn, monkeypatch):
        # An engine that keeps reporting the same finding after redaction
        # (placeholder collision, scanner quirk) must never loop forever
        # or silently ship — the stragglers escalate to review.
        finding = {
            "raw": self.RAW, "rule_id": "slack-bot-token",
            "entropy": 4.4, "line": 1,
        }
        _mock_gate_engines(
            monkeypatch, bl_passes=[[finding], [finding], [finding], [finding]]
        )
        share_id, share = self._share(conn, content=f"leak {self.RAW}")
        export_dir, manifest = export_share_to_disk(conn, share_id, share)

        assert manifest["blocked"] is True
        scan = manifest["redaction_summary"]["secret_scan"]
        assert scan["converged"] is False
        assert scan["rescan_passes"] == 3
        entry = manifest["blocked_sessions"][0]["findings"][0]
        assert entry["tier"] == "review"

    def test_missing_binary_blocks_with_install_hint(self, conn, monkeypatch):
        from clawjournal.redaction import betterleaks, trufflehog

        monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
        monkeypatch.delenv(betterleaks.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(trufflehog, "is_available", lambda: True)
        monkeypatch.setattr(betterleaks, "is_available", lambda: False)
        monkeypatch.setattr(trufflehog, "_scan_text_for_raw_matches", lambda text: [])

        share_id, share = self._share(conn)
        export_dir, manifest = export_share_to_disk(conn, share_id, share)
        assert manifest["blocked"] is True
        assert manifest["block_reason"] == "scanner-not-installed"
        assert "clawjournal betterleaks install" in manifest["block_message"]

    def test_scan_error_blocks_with_deterministic_reason(self, conn, monkeypatch):
        from clawjournal.redaction import betterleaks, trufflehog

        _mock_gate_engines(monkeypatch)
        monkeypatch.setattr(
            betterleaks,
            "scan_file_with_raws",
            lambda path: (
                betterleaks.BetterleaksReport(
                    scanned_path=str(path),
                    scanned_sha256="sha256:0",
                    scan_error="unexpected exit status 2",
                ),
                [],
            ),
        )
        share_id, share = self._share(conn)
        pre_status = self._status(conn, share_id)

        export_dir, manifest = export_share_to_disk(conn, share_id, share)
        assert export_dir is not None
        assert manifest["blocked"] is True
        assert manifest["block_reason"] == "scanner-error"
        assert "unexpected exit status 2" in manifest["block_message"]
        assert self._status(conn, share_id) == pre_status
        assert (export_dir / "secret-scan.json").exists()

    def test_bypass_env_var_recorded_in_manifest(self, conn):
        # Autouse fixture sets the bypass vars — verify the manifest
        # records the bypass so reviewers can tell scanned shares from
        # bypassed ones.
        share_id, share = self._share(conn)
        export_dir, manifest = export_share_to_disk(conn, share_id, share)
        assert manifest.get("blocked") is not True
        scan = manifest["redaction_summary"]["secret_scan"]
        assert scan["bypassed"] is True
        assert manifest["redaction_summary"]["trufflehog"]["bypassed"] is True
