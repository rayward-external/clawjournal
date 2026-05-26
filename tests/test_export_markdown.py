"""Tests for markdown export rendering."""

import json

from clawjournal.export.markdown import render_session_summary


def _summary_session(**overrides):
    session = {
        "session_id": "summary-001",
        "display_title": "Failure scoring review",
        "source": "codex",
        "model": "gpt-5",
        "duration_seconds": 120,
        "ai_quality_score": 3,
        "ai_outcome_badge": "partial",
        "ai_summary": "Reviewed a scoring trace.",
    }
    session.update(overrides)
    return session


def test_summary_renders_failure_analysis_from_detail_dict():
    text = render_session_summary(_summary_session(
        ai_failure_value_score=5,
        ai_failure_attribution="agent_caused",
        ai_failure_modes=json.dumps(["verification_skipped"]),
        ai_recovery_labels=["unrecovered"],
        ai_learning_summary="Testing must match the stated claim.",
        ai_scoring_detail={
            "ai_meta_labels": ["evaluation_measurement"],
            "ai_failure_evidence": ["The agent claimed tests passed without running them."],
        },
    ))

    assert "## Failure Analysis" in text
    assert "- **Failure value:** 5/5" in text
    assert "- **Attribution:** agent_caused" in text
    assert "- **Modes:** verification_skipped" in text
    assert "- **Recovery:** unrecovered" in text
    assert "- **Meta labels:** evaluation_measurement" in text
    assert "_Testing must match the stated claim._" in text
    assert "- The agent claimed tests passed without running them." in text


def test_summary_omits_failure_analysis_without_failure_data():
    text = render_session_summary(_summary_session())

    assert "## Failure Analysis" not in text
