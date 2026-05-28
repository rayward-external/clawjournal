"""Tests for the DB-backed findings path in clawjournal.redaction.secrets."""

from __future__ import annotations

import copy

import pytest

from clawjournal.findings import (
    RawFinding,
    hash_entity,
    reset_salt_cache,
    set_finding_status,
    write_findings_to_db,
)
from clawjournal.redaction.secrets import (
    SECRETS_ENGINE_ID,
    apply_findings_to_blob,
    redact_session,
    scan_session_for_findings,
)
from clawjournal.workbench.index import open_index, upsert_sessions


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    reset_salt_cache()
    connection = open_index()
    yield connection
    connection.close()
    reset_salt_cache()


# A plausible session blob carrying a handful of canonical secrets across
# different fields + tool_uses branches. The actual values are harmless
# (fixture, fake) but match the regex patterns the engine detects.
_FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkphbmUifQ"
    ".abcdefghijABCDEFGH0123456789"
)
_FAKE_ANTHROPIC = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
_FAKE_GITHUB = "ghp_abcdefghijklmnopqrstuvwxyzABCDEF0123"
# Round-4 additions: cover the A2 pattern types end-to-end through the
# DB-backed scan_session_for_findings → write_findings_to_db →
# apply_findings_to_blob path (rounds 1-3 only validated them through
# the legacy redact_text path).
_FAKE_STRIPE = "sk_live_" + "A" * 28
_FAKE_STRIPE_WEBHOOK = "whsec_" + "B" * 32
_FAKE_OPAQUE_BEARER = "Bearer " + "C" * 40  # generic-shape, not JWT


def _session_with_secrets():
    return {
        "session_id": "sess-1",
        "project": "demo",
        "source": "claude",
        "model": "claude-sonnet-4",
        "start_time": "2025-01-01T00:00:00+00:00",
        "end_time": "2025-01-01T00:10:00+00:00",
        "git_branch": "main",
        "display_title": "fix the login bug",
        "messages": [
            {
                "role": "user",
                "content": f"Here's the token: {_FAKE_JWT}\nand the anthropic key: {_FAKE_ANTHROPIC}",
                "thinking": "",
                "tool_uses": [],
            },
            {
                "role": "assistant",
                "content": "I'll reuse the same token below.",
                "thinking": f"Using {_FAKE_JWT} again",
                "tool_uses": [{
                    "tool": "bash",
                    "input": {"command": f"curl -H 'Authorization: token {_FAKE_GITHUB}' api.github.com"},
                    "output": "OK",
                }],
            },
            {
                # Round-4: stripe_key, stripe_webhook_secret, and a
                # generic-shape bearer all flow through the same
                # findings substrate. Pin that they emit RawFinding
                # records and survive write→apply round-trip.
                "role": "assistant",
                "content": (
                    f"Charge with {_FAKE_STRIPE}; webhook signs with "
                    f"{_FAKE_STRIPE_WEBHOOK}; opaque bearer: {_FAKE_OPAQUE_BEARER}"
                ),
                "thinking": "",
                "tool_uses": [],
            },
        ],
        "stats": {
            "user_messages": 1, "assistant_messages": 2, "tool_uses": 1,
            "input_tokens": 100, "output_tokens": 50,
        },
    }


def _seed_session_row(conn):
    upsert_sessions(conn, [_session_with_secrets()])
    conn.commit()


class TestScanSessionForFindings:
    def test_emits_raw_findings_with_offsets(self, conn):
        raw = scan_session_for_findings(_session_with_secrets())
        # At minimum: jwt in msg[0].content, anthropic key in msg[0].content,
        # jwt in msg[1].thinking, github token in msg[1].tool input.
        engines = {f.engine for f in raw}
        assert engines == {SECRETS_ENGINE_ID}
        types = {f.entity_type for f in raw}
        assert {"jwt", "anthropic_key", "github_token"} <= types

        # Every finding has a non-empty match text of the right length and
        # an offset that actually points at the match in the stated field.
        blob = _session_with_secrets()
        for finding in raw:
            text = _field_text(blob, finding)
            assert text is not None
            assert text[finding.offset:finding.offset + finding.length] == finding.entity_text

    def test_no_plaintext_in_hashed_representation(self, conn):
        raw = scan_session_for_findings(_session_with_secrets())
        assert raw, "fixture should produce findings"
        # Simulate a DB write and confirm plaintext doesn't leak into the row.
        _seed_session_row(conn)
        write_findings_to_db(conn, "sess-1", raw, revision="v1:t")
        conn.commit()
        rows = conn.execute("SELECT * FROM findings").fetchall()
        for row in rows:
            dumped = str(dict(row))
            assert _FAKE_JWT not in dumped
            assert _FAKE_ANTHROPIC not in dumped
            assert _FAKE_GITHUB not in dumped

    def test_a2_patterns_emit_raw_findings(self, conn):
        """Round-4: pin that the A2-added patterns (stripe_key,
        stripe_webhook_secret, bearer_generic) flow through the
        DB-backed scan path and produce RawFinding records the same
        way JWT/anthropic/github do. Rounds 1-3 only validated the
        legacy redact_text path; this pins the substrate."""

        raw = scan_session_for_findings(_session_with_secrets())
        types = {f.entity_type for f in raw}
        assert "stripe_key" in types, (
            f"stripe_key missing from findings substrate: types={sorted(types)}"
        )
        assert "stripe_webhook_secret" in types, (
            f"stripe_webhook_secret missing: types={sorted(types)}"
        )
        # The opaque bearer in the fixture gets caught by bearer_generic
        # (no JWT shape, no inner JWT match overlay).
        assert "bearer_generic" in types, (
            f"bearer_generic missing: types={sorted(types)}"
        )

    def test_a2_secrets_no_plaintext_in_db(self, conn):
        """Round-4: write→read round-trip for the A2 patterns;
        plaintext must not appear in any DB column."""

        raw = scan_session_for_findings(_session_with_secrets())
        _seed_session_row(conn)
        write_findings_to_db(conn, "sess-1", raw, revision="v1:t")
        conn.commit()
        rows = conn.execute("SELECT * FROM findings").fetchall()
        for row in rows:
            dumped = str(dict(row))
            assert "sk_live_" + "A" * 28 not in dumped
            assert "whsec_" + "B" * 32 not in dumped
            # The opaque bearer body is 40 C's; a substring check is
            # enough to detect leakage.
            assert "C" * 40 not in dumped


def _field_text(blob, finding):
    if finding.message_index is None:
        return blob.get(finding.field)
    msg = blob["messages"][finding.message_index]
    if finding.field in ("content", "thinking"):
        return msg.get(finding.field)
    # tool_uses[<idx>].<branch>[.<key>]
    import re
    match = re.match(r"tool_uses\[(\d+)\]\.(\w+)(?:\.(.+))?$", finding.field)
    if not match:
        return None
    tool_idx = int(match.group(1))
    branch = match.group(2)
    nested_key = match.group(3)
    tool = msg["tool_uses"][tool_idx]
    val = tool.get(branch)
    if nested_key:
        return val.get(nested_key) if isinstance(val, dict) else None
    return val if isinstance(val, str) else None


class TestApplyFindingsToBlob:
    def test_byte_equivalent_to_redact_session_all_accept(self, conn):
        """When every finding is open/accepted, DB-backed apply matches legacy."""
        _seed_session_row(conn)
        raw = scan_session_for_findings(_session_with_secrets())
        write_findings_to_db(conn, "sess-1", raw, revision="v1:baseline")
        conn.commit()

        legacy_blob, legacy_count, _ = redact_session(_session_with_secrets())
        db_blob, db_count = apply_findings_to_blob(
            _session_with_secrets(), conn, "sess-1",
        )

        assert legacy_blob == db_blob
        assert db_count == legacy_count

    def test_ignored_finding_is_not_redacted(self, conn):
        _seed_session_row(conn)
        raw = scan_session_for_findings(_session_with_secrets())
        write_findings_to_db(conn, "sess-1", raw, revision="v1:rev")
        conn.commit()

        jwt_hash = hash_entity(_FAKE_JWT)
        finding_ids = [
            r["finding_id"] for r in conn.execute(
                "SELECT finding_id FROM findings WHERE entity_hash = ?", (jwt_hash,)
            ).fetchall()
        ]
        assert finding_ids
        set_finding_status(conn, finding_ids, "ignored", reason="fixture")
        conn.commit()

        blob, _count = apply_findings_to_blob(
            _session_with_secrets(), conn, "sess-1",
        )
        # JWT stays in place everywhere it appeared; anthropic + github still redacted.
        assert _FAKE_JWT in blob["messages"][0]["content"]
        assert _FAKE_JWT in blob["messages"][1]["thinking"]
        assert _FAKE_ANTHROPIC not in blob["messages"][0]["content"]
        assert _FAKE_GITHUB not in blob["messages"][1]["tool_uses"][0]["input"]["command"]

    def test_ignoring_one_occurrence_ignores_all(self, conn):
        """Entity-level decision: same hash across multiple occurrences shares one status."""
        _seed_session_row(conn)
        raw = scan_session_for_findings(_session_with_secrets())
        write_findings_to_db(conn, "sess-1", raw, revision="v1:r")
        conn.commit()

        jwt_ids = [
            r["finding_id"] for r in conn.execute(
                "SELECT finding_id FROM findings WHERE entity_hash = ?",
                (hash_entity(_FAKE_JWT),),
            ).fetchall()
        ]
        # JWT appears in msg0.content and msg1.thinking — at least 2 rows.
        assert len(jwt_ids) >= 2

        # Ignore just one; entity-level fan-out should flip all.
        set_finding_status(conn, jwt_ids[:1], "ignored")
        conn.commit()

        statuses = {
            r["status"] for r in conn.execute(
                "SELECT status FROM findings WHERE entity_hash = ?",
                (hash_entity(_FAKE_JWT),),
            ).fetchall()
        }
        assert statuses == {"ignored"}

    def test_apply_without_findings_rows_redacts_for_safety(self, conn):
        """No DB rows for a matched entity → redact anyway (Decision 6).

        The 'should not happen unless ENGINE_VERSION drifted' edge case
        defaults to redaction so a broken scan cannot leak plaintext
        through the share path.
        """
        _seed_session_row(conn)
        blob = _session_with_secrets()
        legacy_blob, legacy_count, _ = redact_session(copy.deepcopy(blob))
        result, count = apply_findings_to_blob(blob, conn, "sess-1")
        # Byte-equivalent to the legacy all-accept path, via the
        # redact-for-safety default.
        assert result == legacy_blob
        assert count == legacy_count

    def test_tool_server_id_redacts_without_findings_rows(self, conn):
        raw = "407e1111222233334444555566660ea2c7fa"
        blob = {
            "session_id": "sess-server-id",
            "project": "demo",
            "source": "claude",
            "model": "claude-sonnet-4",
            "start_time": "2025-01-01T00:00:00+00:00",
            "end_time": "2025-01-01T00:10:00+00:00",
            "git_branch": "main",
            "display_title": "preview test",
            "messages": [{
                "role": "assistant",
                "content": "",
                "thinking": "",
                "tool_uses": [{
                    "tool": "mcp__Claude_Preview__preview_eval",
                    "input": {"serverId": raw, "expression": "location.href = '/login/'"},
                    "output": {"text": f'{{"serverId": "{raw}", "port": 8000}}'},
                }],
            }],
            "stats": {
                "user_messages": 0, "assistant_messages": 1, "tool_uses": 1,
                "input_tokens": 1, "output_tokens": 1,
            },
        }

        result, count = apply_findings_to_blob(copy.deepcopy(blob), conn, "sess-server-id")

        dumped = str(result)
        assert raw not in dumped
        assert count == 2
        assert result["messages"][0]["tool_uses"][0]["input"]["serverId"] == (
            "[REDACTED_TOOL_SERVER_ID]"
        )
        assert "[REDACTED_TOOL_SERVER_ID]" in (
            result["messages"][0]["tool_uses"][0]["output"]["text"]
        )

    def test_multi_pass_catches_cascaded_reveals(self, conn):
        """Redaction equivalence holds for sessions where a secret hides
        inside another. Here we just confirm apply terminates and produces
        the same output as redact_session under all-accept."""
        _seed_session_row(conn)
        blob = _session_with_secrets()
        # Hide the github token inside a "pre-redacted-looking" wrapper that
        # still contains a valid pattern after the first sweep.
        blob["messages"][1]["content"] = f"[wrap {_FAKE_GITHUB} /wrap]"
        raw = scan_session_for_findings(blob)
        write_findings_to_db(conn, "sess-1", raw, revision="v1:multi")
        conn.commit()

        legacy_blob, legacy_count, _ = redact_session(copy.deepcopy(blob))
        db_blob, db_count = apply_findings_to_blob(blob, conn, "sess-1")
        assert legacy_blob == db_blob
        assert db_count == legacy_count
