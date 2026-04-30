"""Redaction-pipeline tests for the widened message model (phase-2 C1).

Invariant under test: secrets in the new fields (``invocations``,
``snippets``, ``extra``, ``author``) get the same redaction treatment
as ``content`` and ``thinking``. The ``extra`` field especially —
phase-2/01 invariant 8 says metadata is not exempt from redaction,
because credentials have been observed nested in agent-emitted JSON.

Tests cover:
- ``redact_session`` (legacy mutate-in-place path).
- ``apply_findings_to_blob`` (DB-backed share-time apply).
- ``review_session_pii`` (rule-based PII scan).
- The 64 KiB ``invocations.arguments`` cap helper.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from clawjournal.findings import write_findings_to_db
from clawjournal.parsing.widened import (
    INVOCATION_ARGUMENTS_CAP_BYTES,
    build_invocation,
    build_snippet,
    iter_widened_text_locations,
    truncate_invocation_arguments,
)
from clawjournal.redaction.pii import review_session_pii
from clawjournal.redaction.secrets import (
    apply_findings_to_blob,
    redact_session,
    scan_session_for_findings,
)


# A real-shape secret that the existing pattern set redacts with high
# confidence. Using sk-ant- because it has a stable, well-understood
# placeholder and high entropy.
ANTHROPIC_KEY = "sk-ant-api03-" + "A" * 90
EXPECTED_PLACEHOLDER = "[REDACTED_ANTHROPIC_KEY]"

STRIPE_KEY = "sk_live_" + "B" * 30
EXPECTED_STRIPE_PLACEHOLDER = "[REDACTED_STRIPE_KEY]"


def _session_with(msg_extras: dict) -> dict:
    """Build a minimal session containing one assistant message with the given fields."""
    msg = {"role": "assistant", "content": "ok"}
    msg.update(msg_extras)
    return {
        "session_id": "test-1",
        "project": "p",
        "source": "claude",
        "messages": [msg],
    }


# ---------------------------------------------------------------------------
# Truncation helper
# ---------------------------------------------------------------------------

class TestTruncateInvocationArguments:
    def test_under_cap_returns_unchanged(self):
        small = {"file": "/tmp/a.txt", "lines": list(range(100))}
        capped, truncated = truncate_invocation_arguments(small)
        assert capped == small
        assert truncated is False

    def test_over_cap_marks_truncated(self):
        # 70 KiB string of distinct content forces the cap to fire.
        big_text = "x" * (70 * 1024)
        capped, truncated = truncate_invocation_arguments({"payload": big_text})
        assert truncated is True
        assert isinstance(capped, str)
        # Truncated form is the leading bytes of the JSON serialization.
        assert len(capped.encode("utf-8")) <= INVOCATION_ARGUMENTS_CAP_BYTES

    def test_string_input_truncated_directly(self):
        big_text = "y" * (80 * 1024)
        capped, truncated = truncate_invocation_arguments(big_text)
        assert truncated is True
        assert capped == big_text[:INVOCATION_ARGUMENTS_CAP_BYTES]

    def test_none_passes_through(self):
        capped, truncated = truncate_invocation_arguments(None)
        assert capped is None
        assert truncated is False

    def test_build_invocation_attaches_marker(self):
        big = {"blob": "z" * (100 * 1024)}
        inv = build_invocation(name="read_file", arguments=big)
        assert inv["arguments_truncated"] is True
        assert isinstance(inv["arguments"], str)

    def test_build_invocation_omits_raw_name_when_redundant(self):
        inv = build_invocation(name="read_file", raw_name="read_file", arguments={"a": 1})
        assert "raw_name" not in inv

    def test_build_invocation_keeps_distinct_raw_name(self):
        inv = build_invocation(name="read_file", raw_name="skill('read_file')", arguments={"a": 1})
        assert inv["raw_name"] == "skill('read_file')"


# ---------------------------------------------------------------------------
# iter_widened_text_locations: traversal and field-label correctness
# ---------------------------------------------------------------------------

class TestIterWidenedTextLocations:
    def test_legacy_message_yields_nothing(self):
        msg = {"role": "user", "content": "hi", "tool_uses": []}
        assert list(iter_widened_text_locations(msg)) == []

    def test_extra_yields_recursively(self):
        msg = {
            "role": "assistant",
            "extra": {"meta": {"trace_id": "abc-123", "nested": {"k": "v"}}},
        }
        out = list(iter_widened_text_locations(msg))
        labels = {label for _, label in out}
        # Recursion produces the deep field labels.
        assert "extra.meta.trace_id" in labels
        assert "extra.meta.nested.k" in labels

    def test_invocations_arguments_yielded(self):
        msg = {
            "role": "assistant",
            "invocations": [build_invocation(name="run", arguments={"cmd": "ls"})],
        }
        out = list(iter_widened_text_locations(msg))
        labels = {label for _, label in out}
        assert "invocations[0].name" in labels
        assert "invocations[0].arguments.cmd" in labels

    def test_author_emitted(self):
        msg = {"role": "assistant", "author": "subagent-1"}
        out = list(iter_widened_text_locations(msg))
        assert ("subagent-1", "author") in out


# ---------------------------------------------------------------------------
# redact_session: secrets in widened fields get redacted
# ---------------------------------------------------------------------------

class TestRedactSessionWidened:
    def test_secret_in_extra_redacted(self):
        # Bare key, no Bearer prefix — the Bearer pattern would absorb
        # the key into a [REDACTED_BEARER] match otherwise. We want to
        # see the typed Anthropic placeholder to prove specificity.
        session = _session_with({
            "extra": {"trace": {"value": ANTHROPIC_KEY}}
        })
        out, count, _log = redact_session(session)
        assert count >= 1
        assert ANTHROPIC_KEY not in json.dumps(out)
        assert EXPECTED_PLACEHOLDER == out["messages"][0]["extra"]["trace"]["value"]

    def test_secret_in_invocations_arguments_redacted(self):
        session = _session_with({
            "invocations": [
                build_invocation(name="curl", arguments={"url": f"https://api/{STRIPE_KEY}"})
            ]
        })
        out, count, _log = redact_session(session)
        assert count >= 1
        assert STRIPE_KEY not in json.dumps(out)
        assert EXPECTED_STRIPE_PLACEHOLDER in out["messages"][0]["invocations"][0]["arguments"]["url"]

    def test_secret_in_snippet_content_redacted(self):
        session = _session_with({
            "snippets": [build_snippet(path="/etc/keys.txt", content=f"key={ANTHROPIC_KEY}")]
        })
        out, _count, _log = redact_session(session)
        assert ANTHROPIC_KEY not in json.dumps(out)
        assert EXPECTED_PLACEHOLDER in out["messages"][0]["snippets"][0]["content"]

    def test_secret_in_author_field_redacted(self):
        # Pathological: an agent puts a secret in the author label.
        session = _session_with({"author": f"sub-{ANTHROPIC_KEY}"})
        out, _count, _log = redact_session(session)
        assert ANTHROPIC_KEY not in out["messages"][0]["author"]
        assert EXPECTED_PLACEHOLDER in out["messages"][0]["author"]

    def test_legacy_message_unchanged(self):
        """Backward compat: messages with no widened fields work as before."""
        session = {
            "session_id": "t",
            "project": "p",
            "source": "claude",
            "messages": [{"role": "user", "content": f"key={ANTHROPIC_KEY}", "tool_uses": []}],
        }
        out, count, _log = redact_session(session)
        assert count >= 1
        assert ANTHROPIC_KEY not in out["messages"][0]["content"]


# ---------------------------------------------------------------------------
# apply_findings_to_blob: DB-backed share-time apply
# ---------------------------------------------------------------------------

class TestApplyFindingsToBlobWidened:
    @pytest.fixture
    def in_memory_db(self):
        from clawjournal.workbench.index import SCHEMA_SQL

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA_SQL)
        return conn

    def test_apply_redacts_secret_in_extra(self, in_memory_db):
        session = _session_with({
            "extra": {"meta": {"value": ANTHROPIC_KEY}}
        })
        raw = scan_session_for_findings(session)
        write_findings_to_db(in_memory_db, "test-1", raw, revision="rev1")
        in_memory_db.commit()

        blob = json.loads(json.dumps(session))
        out, count = apply_findings_to_blob(blob, in_memory_db, "test-1")
        assert count >= 1
        assert ANTHROPIC_KEY not in json.dumps(out)
        assert EXPECTED_PLACEHOLDER == out["messages"][0]["extra"]["meta"]["value"]

    def test_apply_redacts_secret_in_invocations(self, in_memory_db):
        session = _session_with({
            "invocations": [
                build_invocation(name="api", arguments={"token": ANTHROPIC_KEY})
            ]
        })
        raw = scan_session_for_findings(session)
        # Confirm scan found at least one widened-field finding (proves
        # _iter_text_locations reaches invocations).
        widened_fields = [f for f in raw if f.field.startswith("invocations[")]
        assert widened_fields, "scan must locate secrets nested in invocations"

        write_findings_to_db(in_memory_db, "test-1", raw, revision="rev1")
        in_memory_db.commit()

        blob = json.loads(json.dumps(session))
        out, count = apply_findings_to_blob(blob, in_memory_db, "test-1")
        assert count >= 1
        assert ANTHROPIC_KEY not in json.dumps(out)


# ---------------------------------------------------------------------------
# review_session_pii: rule-based PII scan covers widened fields
# ---------------------------------------------------------------------------

class TestReviewSessionPiiWidened:
    def test_email_in_extra_detected(self):
        # Use a clearly-email-shaped string that the rule-based PII
        # matcher classifies. The exact field label is not asserted —
        # only that *some* finding lands inside the widened path.
        session = _session_with({
            "extra": {"sender": "alice@example.com"}
        })
        findings = review_session_pii(session)
        # PIIFinding is a TypedDict; index by key. Without widened-field
        # plumbing this scan would skip extra entirely.
        widened_findings = [f for f in findings if f.get("field", "").startswith("extra")]
        assert widened_findings, "PII scan must reach extra.* fields"
