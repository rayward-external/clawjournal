"""Tests for clawjournal.workbench.card — share card generation."""

import json

import pytest

from clawjournal.workbench.card import (
    MAX_CARD_CHARS,
    _build_card_text,
    _format_duration,
    _format_tokens,
    _short_model_name,
    generate_card,
)


class TestFormatDuration:
    def test_seconds(self):
        assert _format_duration(30) == "30s"

    def test_minutes(self):
        assert _format_duration(120) == "2 min"

    def test_hours(self):
        assert _format_duration(3600) == "1h"

    def test_hours_minutes(self):
        assert _format_duration(5400) == "1h 30m"

    def test_none(self):
        assert _format_duration(None) == ""

    def test_zero(self):
        assert _format_duration(0) == ""


class TestFormatTokens:
    def test_small(self):
        assert _format_tokens(500) == "500"

    def test_thousands(self):
        assert _format_tokens(4200) == "4.2k"

    def test_large(self):
        assert _format_tokens(15000) == "15k"


class TestShortModelName:
    def test_claude_sonnet(self):
        assert _short_model_name("claude-sonnet-4-20250514") == "sonnet-4"

    def test_claude_opus(self):
        assert _short_model_name("claude-opus-4-20250514") == "opus-4"

    def test_already_short(self):
        assert _short_model_name("gpt-4o") == "gpt-4o"

    def test_empty(self):
        assert _short_model_name("") == ""

    def test_no_date_suffix(self):
        assert _short_model_name("claude-haiku-4-5") == "haiku-4-5"

    def test_non_claude_preserved(self):
        assert _short_model_name("gpt-4o-20250101") == "gpt-4o"


class TestBuildCardText:
    def test_basic_card(self):
        card = {
            "title": "Fix auth bug",
            "source": "openclaw",
            "model": "sonnet-4",
            "duration_seconds": 1380,
            "score": 4,
            "outcome": "tests_passed",
            "summary_line": "Fix the authentication bug",
            "workflow_oneliner": "Read python file → Edit python file → Test (4/4 passed)",
            "stats": {
                "user_messages": 5,
                "assistant_messages": 5,
                "tool_uses": 8,
                "total_tokens": 3200,
            },
            "redaction_count": 3,
        }
        text = _build_card_text(card, "summary")
        assert "Fix auth bug" in text
        assert "Openclaw" in text
        assert "sonnet-4" in text
        assert "23 min" in text
        assert "Tests passed" in text
        assert "4/5" in text
        assert "Fix the authentication bug" in text
        assert "Read python file" in text
        assert "10 msgs" in text
        assert "3.2k tokens" in text
        assert "3 secrets redacted" in text

    def test_workflow_no_redaction(self):
        card = {
            "title": "Test session",
            "source": "claude",
            "model": "opus-4",
            "duration_seconds": 300,
            "score": None,
            "outcome": "",
            "summary_line": "",
            "workflow_oneliner": "Read file",
            "stats": {"user_messages": 1, "assistant_messages": 1, "tool_uses": 1, "total_tokens": 500},
            "redaction_count": 5,
        }
        text = _build_card_text(card, "workflow")
        assert "redacted" not in text  # workflow depth hides redaction count

    def test_missing_optional_fields(self):
        card = {
            "title": "",
            "source": "",
            "model": "",
            "duration_seconds": None,
            "score": None,
            "outcome": "",
            "summary_line": "",
            "workflow_oneliner": "",
            "stats": {"user_messages": 0, "assistant_messages": 0, "tool_uses": 0, "total_tokens": 0},
            "redaction_count": 0,
        }
        text = _build_card_text(card, "summary")
        assert "Session" in text  # falls back to "Session"


class TestGenerateCard:
    @pytest.fixture
    def sample_session(self):
        return {
            "session_id": "test-card-123",
            "display_title": "Fix auth bug",
            "source": "openclaw",
            "model": "claude-sonnet-4-20250514",
            "duration_seconds": 1380,
            "ai_quality_score": 4,
            "outcome_badge": "tests_passed",
            "user_messages": 8,
            "assistant_messages": 8,
            "tool_uses": 3,
            "input_tokens": 3200,
            "output_tokens": 1000,
            "_redaction_count": 7,
            "messages": [
                {"role": "user", "content": "Fix the authentication bug"},
                {
                    "role": "assistant",
                    "tool_uses": [
                        {"tool": "Read", "input": {"file_path": "src/auth.py"}, "output": {"text": "code"}, "status": "success"},
                        {"tool": "Edit", "input": {"file_path": "src/auth.py", "old_string": "old", "new_string": "new"}, "output": {}, "status": "success"},
                        {"tool": "Bash", "input": {"command": "pytest"}, "output": {"text": "4 passed"}, "status": "success"},
                    ],
                },
            ],
        }

    def test_generate_summary(self, sample_session):
        result = generate_card(sample_session, "summary")
        assert result["session_id"] == "test-card-123"
        assert result["depth"] == "summary"
        assert "card" in result
        assert "card_text" in result
        assert "next_steps" in result

    def test_card_structure(self, sample_session):
        result = generate_card(sample_session, "summary")
        card = result["card"]
        assert card["title"] == "Fix auth bug"
        assert card["source"] == "openclaw"
        assert card["model"] == "sonnet-4"
        assert card["score"] == 4
        assert card["outcome"] == "tests_passed"
        assert card["redaction_count"] == 7

    def test_failure_card_preserves_structured_labels(self, sample_session):
        sample_session.update({
            "ai_failure_value_score": 4,
            "ai_failure_attribution": "agent_caused",
            "ai_failure_modes": json.dumps([
                "reasoning_fabrication",
                "verification_skipped",
            ]),
        })
        result = generate_card(sample_session, "summary")

        assert result["card"]["failure"] == {
            "score": 4,
            "attribution": "agent_caused",
            "modes": ["reasoning_fabrication", "verification_skipped"],
        }
        assert (
            "Failure 4/5 · agent caused · reasoning fabrication, verification skipped"
            in result["card_text"]
        )

    def test_failure_card_ignores_malformed_mode_payload(self, sample_session):
        sample_session["ai_failure_modes"] = '{"not": "a-list"}'

        result = generate_card(sample_session, "summary")

        assert result["card"]["failure"]["modes"] == []

    def test_card_text_not_empty(self, sample_session):
        result = generate_card(sample_session, "summary")
        assert len(result["card_text"]) > 0

    def test_card_text_within_limit(self, sample_session):
        result = generate_card(sample_session, "summary")
        assert len(result["card_text"]) <= MAX_CARD_CHARS

    def test_workflow_depth(self, sample_session):
        result = generate_card(sample_session, "workflow")
        card = result["card"]
        assert card["summary_line"] == ""

    def test_full_depth(self, sample_session):
        result = generate_card(sample_session, "full")
        card = result["card"]
        assert card["summary_line"]  # should have content

    def test_all_depths_produce_text(self, sample_session):
        for depth in ("workflow", "summary", "full"):
            result = generate_card(sample_session, depth)
            assert result["card_text"], f"card_text empty for depth={depth}"

    def test_card_with_many_steps(self):
        """Card with >7 steps should truncate the oneliner."""
        tools = [
            {"tool": "Read", "input": {"file_path": f"file{i}.py"}, "output": {"text": "x"}, "status": "success"}
            for i in range(15)
        ]
        session = {
            "session_id": "many-steps",
            "display_title": "Many steps session",
            "source": "openclaw",
            "model": "sonnet-4",
            "duration_seconds": 600,
            "ai_quality_score": None,
            "outcome_badge": "unknown",
            "user_messages": 1,
            "assistant_messages": 1,
            "tool_uses": 15,
            "input_tokens": 1000,
            "output_tokens": 500,
            "_redaction_count": 0,
            "messages": [
                {"role": "user", "content": "Do stuff"},
                {"role": "assistant", "tool_uses": tools},
            ],
        }
        result = generate_card(session, "summary")
        assert "more" in result["card_text"]
