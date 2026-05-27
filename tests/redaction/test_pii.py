import json
import subprocess

import pytest

from clawjournal.redaction.pii import (
    _collect_text_work_items,
    _content_findings_for_text,
    _extract_json_array,
    _load_pii_rubric,
    _normalize_llm_findings,
    _read_batch_findings,
    _review_text_with_agent,
    _split_into_batches,
    _truncate_for_llm,
    apply_findings_to_session,
    apply_findings_to_text,
    load_findings,
    merge_findings,
    replacement_for_type,
    review_session_pii,
    review_session_pii_hybrid,
    review_session_pii_with_agent,
    write_findings,
)


def test_replacement_for_type_defaults():
    assert replacement_for_type("person_name") == "[REDACTED_NAME]"
    assert replacement_for_type("unknown") == "[REDACTED]"


def test_merge_findings_prefers_longer_text():
    findings = [
        {
            "session_id": "s1",
            "message_index": 0,
            "field": "content",
            "entity_text": "Jane",
            "entity_type": "person_name",
            "confidence": 0.8,
        },
        {
            "session_id": "s1",
            "message_index": 0,
            "field": "content",
            "entity_text": "Jane D",
            "entity_type": "person_name",
            "confidence": 0.95,
        },
    ]
    merged = merge_findings(findings)
    assert len(merged) == 1
    assert merged[0]["entity_text"] == "Jane D"


def test_apply_findings_to_text_replaces_all_occurrences():
    findings = [
        {
            "entity_text": "Jane D",
            "entity_type": "person_name",
            "replacement": "[REDACTED_NAME]",
            "confidence": 0.9,
        }
    ]
    redacted, count = apply_findings_to_text("Jane D said hi to Jane D.", findings)
    assert count == 2
    assert "Jane D" not in redacted
    assert redacted.count("[REDACTED_NAME]") == 2


def test_apply_findings_to_session_nested_tool_field():
    session = {
        "session_id": "s1",
        "messages": [
            {
                "content": "hello",
                "tool_uses": [
                    {
                        "input": {"command": "echo Jane D"},
                        "output": {"text": "done"},
                    }
                ],
            }
        ],
    }
    findings = [
        {
            "session_id": "s1",
            "message_index": 0,
            "field": "tool_uses[0].input.command",
            "entity_text": "Jane D",
            "entity_type": "person_name",
            "replacement": "[REDACTED_NAME]",
            "confidence": 0.99,
        }
    ]
    redacted, count = apply_findings_to_session(session, findings)
    assert count == 1
    assert redacted["messages"][0]["tool_uses"][0]["input"]["command"] == "echo [REDACTED_NAME]"


def test_apply_findings_to_session_redacts_scoring_text():
    session = {
        "session_id": "s1",
        "ai_learning_summary": "Jane D corrected the paired-data assumption.",
        "ai_scoring_detail": json.dumps({
            "reasoning": "Jane D corrected the agent.",
            "ai_failure_evidence": ["Jane D pointed out the samples were paired."],
        }),
        "messages": [],
    }
    findings = [
        {
            "session_id": "s1",
            "message_index": -1,
            "field": "ai_learning_summary",
            "entity_text": "Jane D",
            "entity_type": "person_name",
            "replacement": "[REDACTED_NAME]",
            "confidence": 0.99,
        }
    ]
    redacted, count = apply_findings_to_session(session, findings)
    detail = json.loads(redacted["ai_scoring_detail"])
    assert count == 3
    assert "Jane D" not in redacted["ai_learning_summary"]
    assert "Jane D" not in detail["reasoning"]
    assert "Jane D" not in detail["ai_failure_evidence"][0]


def test_collect_text_work_items_includes_scoring_text():
    session = {
        "session_id": "s1",
        "project": "Private Project",
        "messages": [{"content": "regular text"}],
        "ai_learning_summary": "Jane D corrected the assumption.",
        "ai_scoring_detail": json.dumps({
            "reasoning": "Acme Lab was mentioned.",
            "ai_failure_evidence": ["Jane D supplied the constraint."],
        }),
    }
    fields = {field for _sid, _idx, field, _text in _collect_text_work_items(session)}
    assert "project" in fields
    assert "ai_learning_summary" in fields
    assert "ai_scoring_detail.reasoning" in fields
    assert "ai_scoring_detail.ai_failure_evidence[0]" in fields


def test_review_session_pii_scans_scoring_text():
    session = {
        "session_id": "s1",
        "messages": [],
        "ai_learning_summary": "The user at jane@example.com corrected the agent.",
        "ai_scoring_detail": json.dumps({
            "ai_failure_evidence": ["Path /Users/jane/private-lab/notes.md leaked."],
        }),
    }
    findings = review_session_pii(session)
    entity_texts = {f["entity_text"] for f in findings}

    assert "jane@example.com" in entity_texts
    assert "/Users/jane/private-lab/notes.md" in entity_texts


def test_review_session_pii_detects_metadata_entities():
    session = {
        "session_id": "s1",
        "messages": [
            {
                "content": '{"sender_id":"7859110712","name":"Jane D","username":"janedoe42"}'
            }
        ],
    }
    findings = review_session_pii(session)
    entity_texts = {f["entity_text"] for f in findings}
    assert "7859110712" in entity_texts
    assert "Jane D" in entity_texts
    assert "janedoe42" in entity_texts


def test_review_session_pii_detects_escaped_quote_metadata():
    """Metadata patterns should match both regular and escaped JSON quotes."""
    session = {
        "session_id": "s1",
        "messages": [
            {
                "content": r'{"text": "{\"name\":\"Jane D\",\"username\":\"janedoe42\"}"}'
            }
        ],
    }
    findings = review_session_pii(session)
    entity_texts = {f["entity_text"] for f in findings}
    assert "Jane D" in entity_texts
    assert "janedoe42" in entity_texts


def test_metadata_id_pattern_only_matches_numeric_ids():
    """The generic 'id' pattern should only match numeric IDs, not UUIDs or hashes."""
    session = {
        "session_id": "s1",
        "messages": [
            {
                "content": '{"id":"abc123-uuid-value","user_id":"9876543210"}'
            }
        ],
    }
    findings = review_session_pii(session)
    entity_texts = {f["entity_text"] for f in findings}
    # UUID-like id should NOT be matched
    assert "abc123-uuid-value" not in entity_texts
    # Numeric user_id SHOULD be matched via the specific user_id pattern
    assert "9876543210" in entity_texts


def test_write_and_load_findings_roundtrip(tmp_path):
    path = tmp_path / "findings.json"
    findings = [
        {
            "session_id": "s1",
            "message_index": 0,
            "field": "content",
            "entity_text": "Jane D",
            "entity_type": "person_name",
            "confidence": 0.9,
        }
    ]
    write_findings(path, findings, meta={"provider": "rules"})
    loaded = load_findings(path)
    assert len(loaded) == 1
    assert loaded[0]["entity_text"] == "Jane D"
    assert json.loads(path.read_text())["provider"] == "rules"


def test_extract_json_array_from_wrapped_text():
    parsed = _extract_json_array('noise before [{"entity_text":"Jane D","entity_type":"person_name","confidence":0.9,"reason":"name"}] noise after')
    assert parsed[0]["entity_text"] == "Jane D"


def test_normalize_llm_findings_filters_unknown_type():
    findings = _normalize_llm_findings("s1", 0, "content", [{
        "entity_text": "secret thing",
        "entity_type": "weird_type",
        "confidence": 0.7,
        "reason": "sensitive",
    }], source="claude")
    assert findings[0]["entity_type"] == "custom_sensitive"
    assert findings[0]["source"] == "claude"


def test_review_session_pii_with_agent_claude_backend(monkeypatch):
    """review_session_pii_with_agent(backend='claude') dispatches correctly."""
    session = {
        "session_id": "s1",
        "messages": [{"content": "Jane D from Acme"}],
    }

    def fake_batch(session_id, work_items, *, rubric=None, backend="auto", timeout_seconds=180):
        assert session_id == "s1"
        assert len(work_items) == 1
        assert work_items[0][2] == "content"  # field
        assert work_items[0][3] == "Jane D from Acme"  # text
        return [{
            "session_id": session_id,
            "message_index": 0,
            "field": "content",
            "entity_text": "Jane D",
            "entity_type": "person_name",
            "confidence": 0.95,
            "reason": "name",
            "replacement": "[REDACTED_NAME]",
            "source": "claude",
        }]

    monkeypatch.setattr("clawjournal.redaction.pii._review_batch", fake_batch)
    findings = review_session_pii_with_agent(session, backend="claude")
    assert findings[0]["entity_text"] == "Jane D"
    assert findings[0]["source"] == "claude"


def test_review_session_pii_with_agent_dispatches_backend(monkeypatch):
    """review_session_pii_with_agent resolves backend and calls the batch runner."""
    session = {
        "session_id": "s1",
        "messages": [{"content": "Hello World"}],
    }
    captured = {}

    def fake_runner(session_id, work_items, *, rubric=None, backend="auto", timeout_seconds=180):
        captured["session_id"] = session_id
        captured["backend"] = backend
        return []

    monkeypatch.setattr("clawjournal.redaction.pii._review_batch", fake_runner)
    review_session_pii_with_agent(session, backend="codex")
    assert captured["session_id"] == "s1"
    assert captured["backend"] == "codex"


def test_review_session_pii_hybrid_merges_rule_and_claude(monkeypatch):
    session = {
        "session_id": "s1",
        "messages": [{"content": '{"name":"Jane D","username":"janedoe42"} and Acme Labs'}],
    }

    monkeypatch.setattr("clawjournal.redaction.pii.review_session_pii_with_agent", lambda s, backend="auto", ignore_errors=True, **kw: [{
        "session_id": "s1",
        "message_index": 0,
        "field": "content",
        "entity_text": "Acme Labs",
        "entity_type": "org_name",
        "confidence": 0.88,
        "reason": "org",
        "replacement": "[REDACTED_ORG]",
        "source": "claude",
    }])
    findings = review_session_pii_hybrid(session)
    entity_texts = {f["entity_text"] for f in findings}
    assert "Jane D" in entity_texts
    assert "janedoe42" in entity_texts
    assert "Acme Labs" in entity_texts


def test_review_session_pii_hybrid_passes_agent_timeout(monkeypatch):
    session = {"session_id": "s1", "messages": [{"content": "Hello"}]}
    captured = {}

    def fake_agent(session, **kwargs):
        captured["timeout_seconds"] = kwargs.get("timeout_seconds")
        return []

    monkeypatch.setattr("clawjournal.redaction.pii.review_session_pii_with_agent", fake_agent)
    findings = review_session_pii_hybrid(session, timeout_seconds=37)

    assert findings == []
    assert captured["timeout_seconds"] == 37


def test_content_findings_github_url():
    findings = _content_findings_for_text("s1", 0, "content", "See https://github.com/kai-rayward/clawjournal for details")
    entity_texts = {f["entity_text"] for f in findings}
    assert "kai-rayward" in entity_texts
    assert all(f["entity_type"] == "username" for f in findings)


def test_content_findings_github_raw_url():
    findings = _content_findings_for_text("s1", 0, "content", "https://raw.githubusercontent.com/myuser/repo/main/file.txt")
    assert any(f["entity_text"] == "myuser" for f in findings)


def test_content_findings_skips_public_github_orgs():
    findings = _content_findings_for_text("s1", 0, "content", "https://github.com/anthropic/sdk and https://github.com/npm/cli")
    entity_texts = {f["entity_text"] for f in findings}
    assert "anthropic" not in entity_texts
    assert "npm" not in entity_texts


def test_content_findings_skips_rayward_external_org():
    findings = _content_findings_for_text("s1", 0, "content", "git clone https://github.com/rayward-external/clawjournal.git")
    entity_texts = {f["entity_text"] for f in findings}
    assert "rayward-external" not in entity_texts


def test_content_findings_telegram_bot_token():
    findings = _content_findings_for_text("s1", 0, "content", "token is 8773626713:AAGaVClH6X8qr59wwbwoLpqVyu3ebOi5irw here")
    assert len(findings) == 1
    assert findings[0]["entity_type"] == "custom_sensitive"
    assert "8773626713:" in findings[0]["entity_text"]


def test_content_findings_private_ips():
    text = "connect to 10.0.0.1 or 192.168.1.100 or 172.16.0.5 but not 8.8.8.8"
    findings = _content_findings_for_text("s1", 0, "content", text)
    entity_texts = {f["entity_text"] for f in findings}
    assert "10.0.0.1" in entity_texts
    assert "192.168.1.100" in entity_texts
    assert "172.16.0.5" in entity_texts
    assert "8.8.8.8" not in entity_texts


def test_content_findings_email():
    findings = _content_findings_for_text("s1", 0, "content", "contact kai@example.com for help")
    assert any(f["entity_text"] == "kai@example.com" for f in findings)


def test_content_findings_skips_noreply_emails():
    findings = _content_findings_for_text("s1", 0, "content", "Co-Authored-By: noreply@anthropic.com")
    entity_texts = {f["entity_text"] for f in findings}
    assert "noreply@anthropic.com" not in entity_texts


def test_content_findings_session_wide_apply():
    """Findings from one field should redact the same entity in other fields."""
    session = {
        "session_id": "s1",
        "messages": [
            {
                "content": "See https://github.com/kai-rayward/clawjournal",
                "tool_uses": [
                    {"input": {"command": "gh repo view kai-rayward/clawjournal"}, "output": {"text": "ok"}}
                ],
            }
        ],
    }
    findings = review_session_pii(session)
    redacted, count = apply_findings_to_session(session, findings)
    # The entity was found in content; session-wide apply should also redact it in tool input
    assert "kai-rayward" not in redacted["messages"][0]["tool_uses"][0]["input"]["command"]
    assert count >= 2


def test_truncate_for_llm_keeps_ends():
    text = "A" * 7000 + "MIDDLE" + "B" * 7000
    out = _truncate_for_llm(text, max_chars=100)
    assert "TRUNCATED FOR PII REVIEW" in out
    assert out.startswith("A")
    assert out.endswith("B" * 50)


def test_load_pii_rubric_falls_back_to_full_builtin_rubric(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr("clawjournal.redaction.pii._PII_RUBRIC_FILE", Path("/definitely/missing/rubric.md"))
    rubric = _load_pii_rubric()
    assert "What to flag" in rubric
    assert "custom_sensitive" in rubric
    assert "Output schema" in rubric


# ---------------------------------------------------------------------------
# _collect_text_work_items
# ---------------------------------------------------------------------------


def test_collect_text_work_items_basic():
    session = {
        "session_id": "s1",
        "messages": [
            {"content": "hello", "thinking": "thought"},
            {"content": "world"},
        ],
    }
    items = _collect_text_work_items(session)
    assert len(items) == 3
    assert items[0] == ("s1", 0, "content", "hello")
    assert items[1] == ("s1", 0, "thinking", "thought")
    assert items[2] == ("s1", 1, "content", "world")


def test_collect_text_work_items_tool_uses():
    session = {
        "session_id": "s1",
        "messages": [{
            "content": "cmd",
            "tool_uses": [
                {"input": {"command": "echo hi"}, "output": "done"},
            ],
        }],
    }
    items = _collect_text_work_items(session)
    fields = [item[2] for item in items]
    assert "content" in fields
    assert "tool_uses[0].input.command" in fields
    assert "tool_uses[0].output" in fields


def test_collect_text_work_items_skips_empty():
    session = {"session_id": "s1", "messages": [{"content": "   "}]}
    assert _collect_text_work_items(session) == []


def test_collect_text_work_items_non_list_messages():
    session = {"session_id": "s1", "messages": "not a list"}
    assert _collect_text_work_items(session) == []


# ---------------------------------------------------------------------------
# _read_batch_findings
# ---------------------------------------------------------------------------


def test_read_batch_findings_prefers_file(tmp_path):
    (tmp_path / "findings.json").write_text(
        '[{"message_index": 0, "field": "content", "entity_text": "File", "entity_type": "person_name", "confidence": 0.9, "reason": "from file"}]'
    )
    results = _read_batch_findings(tmp_path, "s1", "claude", stdout="[]")
    assert results[0]["entity_text"] == "File"


def test_read_batch_findings_falls_back_to_stdout(tmp_path):
    stdout = '[{"message_index": 0, "field": "content", "entity_text": "Bob", "entity_type": "person_name", "confidence": 0.9, "reason": "name"}]'
    results = _read_batch_findings(tmp_path, "s1", "claude", stdout=stdout)
    assert len(results) == 1
    assert results[0]["entity_text"] == "Bob"


# ---------------------------------------------------------------------------
# _split_into_batches
# ---------------------------------------------------------------------------


def test_split_into_batches_single():
    items = [("s1", 0, "content", "short text")]
    batches = _split_into_batches(items, char_limit=100_000)
    assert len(batches) == 1
    assert batches[0] == items


def test_split_into_batches_splits_large():
    # Each item is 12K after MAX_LLM_TEXT_CHARS cap; 5 items = 60K
    items = [("s1", i, "content", "x" * 40_000) for i in range(5)]
    batches = _split_into_batches(items, char_limit=30_000)
    assert len(batches) >= 2
    # All items accounted for
    assert sum(len(b) for b in batches) == 5


# ---------------------------------------------------------------------------
# _review_text_with_agent dispatch
# ---------------------------------------------------------------------------


def test_review_text_with_agent_unsupported_backend(monkeypatch):
    monkeypatch.setattr("clawjournal.redaction.pii.resolve_backend", lambda b: "gemini")

    def raise_unsupported(**kw):
        raise RuntimeError(f"Unsupported backend: {kw.get('backend', 'gemini')}")

    monkeypatch.setattr("clawjournal.redaction.pii.run_default_agent_task", raise_unsupported)
    with pytest.raises(RuntimeError, match="Unsupported backend"):
        _review_text_with_agent("s1", 0, "content", "test", backend="gemini")


# ---------------------------------------------------------------------------
# review_session_pii_with_agent error propagation
# ---------------------------------------------------------------------------


def test_review_session_pii_with_agent_raises_on_error(monkeypatch):
    session = {"session_id": "s1", "messages": [{"content": "test"}]}

    def failing_runner(*a, **kw):
        raise RuntimeError("backend crashed")

    monkeypatch.setattr("clawjournal.redaction.pii._review_batch", failing_runner)
    with pytest.raises(RuntimeError, match="backend crashed"):
        review_session_pii_with_agent(session, backend="claude", ignore_errors=False)


def test_review_session_pii_with_agent_ignores_errors(monkeypatch):
    session = {"session_id": "s1", "messages": [{"content": "test"}]}

    def failing_runner(*a, **kw):
        raise RuntimeError("backend crashed")

    monkeypatch.setattr("clawjournal.redaction.pii._review_batch", failing_runner)
    findings = review_session_pii_with_agent(session, backend="claude", ignore_errors=True)
    assert findings == []
