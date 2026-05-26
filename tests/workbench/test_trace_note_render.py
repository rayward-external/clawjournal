"""Tests for trace note rendering."""

import json

from clawjournal.workbench.trace_note import (
    SCHEMA_VERSION,
    _normalize_notes,
    extract_rendered_updated_at,
    extract_trace_note_notes,
    render_trace_note,
)


def _minimal_session(**overrides):
    base = {
        "session_id": "01HV7K4M9B1234567890ABCDEF",
        "display_title": "Redesign Share queue flow",
        "project": "clawjournal",
        "source": "claude",
        "model": "claude-opus-4-7",
        "start_time": "2026-04-17T10:34:21+00:00",
        "end_time": "2026-04-17T11:02:44+00:00",
        "duration_seconds": 1703,
        "input_tokens": 12042,
        "output_tokens": 3418,
        "ai_quality_score": 5,
        "ai_effort_estimate": 0.82,
        "ai_outcome_badge": "shipped",
        "ai_value_badges": '["feature", "docs"]',
        "ai_risk_badges": "[]",
        "ai_summary": (
            "Split the monolithic Share page into Queue → Privacy → Done."
        ),
        "updated_at": "2026-04-17T11:07:12+00:00",
    }
    base.update(overrides)
    return base


class TestRenderFullState:
    def test_renders_all_sections(self):
        text = render_trace_note(_minimal_session(), "need to revisit ranking")

        assert "<!-- clawjournal-trace-note v1 -->" in text
        assert "<!-- session_id: 01HV7K4M9B1234567890ABCDEF -->" in text
        assert (
            "<!-- rendered_updated_at: 2026-04-17T11:07:12+00:00 -->" in text
        )
        assert "# Redesign Share queue flow" in text
        # Metadata bullets
        assert "- **Session:** `01HV7K4M9B1234567890ABCDEF`" in text
        assert "- **Project:** clawjournal" in text
        assert "- **Source:** claude (`claude-opus-4-7`)" in text
        assert "- **Tokens:** 12,042 in / 3,418 out" in text
        assert "- **Score:** 5/5 (effort 0.82)" in text
        assert "- **Outcome:** shipped" in text
        assert "- **Tags:** feature, docs" in text
        assert "## Summary" in text
        assert "## Notes" in text
        assert "need to revisit ranking" in text

    def test_ends_with_single_trailing_newline(self):
        text = render_trace_note(_minimal_session(), "x")
        assert text.endswith("\n")
        assert not text.endswith("\n\n")


class TestRenderEmptySummary:
    def test_empty_ai_summary_keeps_section(self):
        text = render_trace_note(_minimal_session(ai_summary=None), None)
        assert "## Summary" in text
        assert "## Notes" in text
        # Title alone still identifies the trace
        assert "# Redesign Share queue flow" in text

    def test_empty_summary_and_notes(self):
        text = render_trace_note(_minimal_session(ai_summary=""), "")
        # Both heading anchors still emitted
        assert text.count("## Summary") == 1
        assert text.count("## Notes") == 1


class TestFailureAnalysis:
    def test_renders_failure_analysis_from_columns_and_detail_dict(self):
        session = _minimal_session(
            ai_failure_value_score=4,
            ai_failure_attribution="agent_caused",
            ai_failure_modes=json.dumps(["reasoning_fabrication"]),
            ai_recovery_labels=["user_corrected_recovery"],
            ai_learning_summary="Verify the evidence before committing.",
            ai_scoring_detail={
                "ai_meta_labels": ["evaluation_measurement"],
                "ai_failure_evidence": ["User corrected a fabricated API."],
            },
        )

        text = render_trace_note(session, "keep this reviewer note")

        assert "## Failure analysis" in text
        assert "- **Failure value:** 4/5" in text
        assert "- **Attribution:** agent_caused" in text
        assert "- **Modes:** reasoning_fabrication" in text
        assert "- **Recovery:** user_corrected_recovery" in text
        assert "- **Meta labels:** evaluation_measurement" in text
        assert "_Verify the evidence before committing._" in text
        assert "- User corrected a fabricated API." in text
        assert extract_trace_note_notes(text) == "keep this reviewer note"

    def test_omits_failure_analysis_when_no_failure_fields(self):
        text = render_trace_note(_minimal_session(), None)

        assert "## Failure analysis" not in text


class TestRenderIdempotent:
    def test_render_twice_is_byte_identical(self):
        s = _minimal_session()
        t1 = render_trace_note(s, "my note")
        t2 = render_trace_note(s, "my note")
        assert t1 == t2

    def test_render_empty_twice_is_byte_identical(self):
        s = _minimal_session(ai_summary=None)
        t1 = render_trace_note(s, None)
        t2 = render_trace_note(s, None)
        assert t1 == t2


class TestMetadataListStability:
    def test_all_bullets_present_when_fields_missing(self):
        # Every score/badge field None → still emit every bullet (with —)
        sparse = {
            "session_id": "sparse-001",
            "updated_at": "2026-04-17T00:00:00+00:00",
        }
        text = render_trace_note(sparse, None)

        for label in [
            "- **Session:**",
            "- **Project:**",
            "- **Source:**",
            "- **When:**",
            "- **Tokens:**",
            "- **Score:**",
            "- **Outcome:**",
            "- **Tags:**",
        ]:
            assert label in text, f"missing stable bullet {label}"
        # Missing values render as —
        assert "- **Project:** —" in text
        assert "- **Source:** —" in text
        assert "- **Tokens:** —" in text
        assert "- **Score:** —" in text
        assert "- **Outcome:** —" in text
        assert "- **Tags:** —" in text

    def test_bullet_order_is_fixed_across_files(self):
        def bullet_labels(text: str) -> list[str]:
            return [
                line for line in text.splitlines() if line.startswith("- **")
            ]

        full = render_trace_note(_minimal_session(), "x")
        sparse = render_trace_note(
            {"session_id": "s", "updated_at": "2026-01-01T00:00:00+00:00"},
            None,
        )
        # The bullets in both files appear in the same order
        labels_full = [b.split("**")[1] for b in bullet_labels(full)]
        labels_sparse = [b.split("**")[1] for b in bullet_labels(sparse)]
        assert labels_full == labels_sparse


class TestTitleNoAiDisplayTitleFallback:
    def test_uses_display_title_only(self):
        # If display_title is set and ai_display_title differs, display_title wins.
        s = _minimal_session(
            display_title="ingest title",
            ai_display_title="LLM-rewritten title",
        )
        text = render_trace_note(s, None)
        assert "# ingest title" in text
        assert "# LLM-rewritten title" not in text

    def test_falls_back_to_session_id_when_no_display_title(self):
        s = _minimal_session(display_title=None, ai_display_title=None)
        text = render_trace_note(s, None)
        assert "# 01HV7K4M9B1234567890ABCDEF" in text


class TestOutcomeBadgePromotion:
    def test_prefers_ai_outcome_over_heuristic(self):
        # Matches API behavior at daemon.py:302-329.
        s = _minimal_session(
            ai_outcome_badge="shipped",
            outcome_badge="errored",
        )
        text = render_trace_note(s, None)
        assert "- **Outcome:** shipped" in text

    def test_falls_back_to_heuristic(self):
        s = _minimal_session(ai_outcome_badge=None, outcome_badge="partial")
        text = render_trace_note(s, None)
        assert "- **Outcome:** partial" in text


class TestBadgesParsing:
    def test_parses_json_encoded_list(self):
        s = _minimal_session(
            ai_value_badges='["feature", "refactor"]',
            ai_risk_badges='["security"]',
        )
        text = render_trace_note(s, None)
        assert "- **Tags:** feature, refactor, security" in text

    def test_accepts_raw_list(self):
        s = _minimal_session(
            ai_value_badges=["a", "b"],
            ai_risk_badges=["c"],
        )
        text = render_trace_note(s, None)
        assert "- **Tags:** a, b, c" in text

    def test_malformed_json_renders_dash(self):
        s = _minimal_session(
            ai_value_badges="not-json",
            ai_risk_badges="also-not-json",
            value_badges=None,
            risk_badges=None,
        )
        text = render_trace_note(s, None)
        assert "- **Tags:** —" in text


class TestRenderedUpdatedAtExtraction:
    def test_extract_matches_stamped_value(self):
        text = render_trace_note(_minimal_session(), None)
        assert (
            extract_rendered_updated_at(text) == "2026-04-17T11:07:12+00:00"
        )

    def test_extract_returns_none_when_comment_missing(self):
        assert extract_rendered_updated_at("# title\n## Notes\n") is None


class TestNormalizeNotes:
    def test_none_becomes_empty(self):
        assert _normalize_notes(None) == ""

    def test_crlf_becomes_lf(self):
        assert _normalize_notes("a\r\nb\r\n") == "a\nb"

    def test_bare_cr_becomes_lf(self):
        assert _normalize_notes("a\rb\r") == "a\nb"

    def test_rstrips_trailing_whitespace(self):
        assert _normalize_notes("hello   \n\n\n") == "hello"

    def test_preserves_internal_blank_lines(self):
        assert _normalize_notes("a\n\nb\n") == "a\n\nb"

    def test_preserves_leading_whitespace(self):
        assert _normalize_notes("    indented\n") == "    indented"

    def test_empty_string_stays_empty(self):
        assert _normalize_notes("") == ""

    def test_whitespace_only_normalizes_to_empty(self):
        assert _normalize_notes("   \n\n  \n") == ""
