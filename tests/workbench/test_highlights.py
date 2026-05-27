"""Tests for get_highlights — dashboard curation endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from clawjournal.workbench.index import get_highlights, open_index, update_session


@pytest.fixture
def index_conn(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db"
    )
    monkeypatch.setattr(
        "clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs"
    )
    conn = open_index()
    yield conn
    conn.close()


def _insert(
    conn,
    *,
    session_id,
    source="claude",
    project="proj-1",
    quality=5,
    failure_value=None,
    ended_hours_ago=24,
    duration=1000,
    title=None,
    summary=None,
):
    end = datetime.now(timezone.utc) - timedelta(hours=ended_hours_ago)
    start = end - timedelta(seconds=duration)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO sessions (
            session_id, project, source, model,
            start_time, end_time, duration_seconds, git_branch,
            user_messages, assistant_messages, tool_uses,
            input_tokens, output_tokens,
            display_title, outcome_badge, value_badges, risk_badges,
            review_status, blob_path, raw_source_path,
            indexed_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id, project, source, f"{source}-model",
            start.isoformat(), end.isoformat(), duration, "main",
            1, 1, 0, 100, 200,
            title or f"Title for {session_id}", "resolved", "[]", "[]",
            "new", f"blobs/{session_id}.json", f"/fake/{session_id}",
            now, now,
        ),
    )
    conn.commit()
    if quality is not None or failure_value is not None:
        if failure_value is None:
            failure_value = quality
        update_session(
            conn, session_id,
            ai_quality_score=quality,
            ai_failure_value_score=failure_value,
            ai_summary=summary or f"Summary for {session_id}",
            ai_learning_summary=summary or f"Summary for {session_id}",
            ai_outcome_badge="resolved",
            ai_effort_estimate=0.5,
        )


class TestBasicSelection:
    def test_picks_top_n_five_star(self, index_conn):
        for i in range(5):
            _insert(index_conn, session_id=f"s{i}", quality=5,
                    ended_hours_ago=i + 1, source="claude")

        data = get_highlights(index_conn, days=7, top_n=3, min_quality=4)

        assert len(data["highlights"]) == 3
        # Most recent end_time first within the same quality tier
        ids = [h["session_id"] for h in data["highlights"]]
        assert ids == ["s0", "s1", "s2"]

    def test_respects_min_quality(self, index_conn):
        _insert(index_conn, session_id="low", quality=3, ended_hours_ago=1)
        _insert(index_conn, session_id="high", quality=5, ended_hours_ago=2)

        data = get_highlights(index_conn, days=7, top_n=3, min_quality=4)

        ids = [h["session_id"] for h in data["highlights"]]
        assert ids == ["high"]

    def test_respects_window_days(self, index_conn):
        _insert(index_conn, session_id="old", quality=5, ended_hours_ago=24 * 20)
        _insert(index_conn, session_id="new", quality=5, ended_hours_ago=24)

        data = get_highlights(index_conn, days=7, top_n=3, min_quality=4)

        ids = [h["session_id"] for h in data["highlights"]]
        assert ids == ["new"]
        assert data["candidate_count"] == 1

    def test_empty_when_no_matches(self, index_conn):
        _insert(index_conn, session_id="low", quality=2, ended_hours_ago=1)

        data = get_highlights(index_conn, days=7, top_n=3, min_quality=4)

        assert data["highlights"] == []
        assert data["candidate_count"] == 0


class TestSourceDiversification:
    def test_prefers_one_per_source(self, index_conn):
        # 5 claude + 2 codex, all five-star. Expect 1 codex + 2 claude (diversify),
        # not 3 claude.
        for i in range(5):
            _insert(index_conn, session_id=f"c{i}", source="claude",
                    quality=5, ended_hours_ago=i + 1)
        _insert(index_conn, session_id="x0", source="codex",
                quality=5, ended_hours_ago=10)
        _insert(index_conn, session_id="x1", source="codex",
                quality=5, ended_hours_ago=11)

        data = get_highlights(index_conn, days=7, top_n=3, min_quality=4)

        sources = [h["source"] for h in data["highlights"]]
        assert "claude" in sources
        assert "codex" in sources
        # First pass picks top claude (c0) and top codex (x0); second pass
        # takes the next best leftover (c1).
        ids = [h["session_id"] for h in data["highlights"]]
        assert ids == ["c0", "x0", "c1"]

    def test_three_sources_all_represented(self, index_conn):
        _insert(index_conn, session_id="c0", source="claude", quality=5, ended_hours_ago=1)
        _insert(index_conn, session_id="x0", source="codex", quality=5, ended_hours_ago=2)
        _insert(index_conn, session_id="o0", source="openclaw", quality=5, ended_hours_ago=3)
        # Plus two more claude that SHOULD lose to diversification
        _insert(index_conn, session_id="c1", source="claude", quality=5, ended_hours_ago=4)
        _insert(index_conn, session_id="c2", source="claude", quality=5, ended_hours_ago=5)

        data = get_highlights(index_conn, days=7, top_n=3, min_quality=4)

        sources = {h["source"] for h in data["highlights"]}
        assert sources == {"claude", "codex", "openclaw"}

    def test_fallback_to_single_source_when_only_one(self, index_conn):
        for i in range(5):
            _insert(index_conn, session_id=f"c{i}", source="claude",
                    quality=5, ended_hours_ago=i + 1)

        data = get_highlights(index_conn, days=7, top_n=3, min_quality=4)

        # Only source has candidates → still pick 3, all from that source.
        assert len(data["highlights"]) == 3
        assert {h["source"] for h in data["highlights"]} == {"claude"}


class TestFailureValueTieBreaker:
    def test_higher_failure_value_beats_more_recent_at_same_source(self, index_conn):
        _insert(index_conn, session_id="solid", quality=5, failure_value=4, ended_hours_ago=1)
        _insert(index_conn, session_id="canonical", quality=3, failure_value=5, ended_hours_ago=24)

        data = get_highlights(index_conn, days=7, top_n=3, min_quality=4)

        ids = [h["session_id"] for h in data["highlights"]]
        # Both are from the same source; failure-value tier wins.
        assert ids == ["canonical", "solid"]


class TestMetadataFields:
    def test_cards_include_display_fields(self, index_conn):
        _insert(
            index_conn, session_id="s1", quality=5, ended_hours_ago=2,
            title="My important trace", summary="Did a big refactor.",
        )

        data = get_highlights(index_conn, days=7, top_n=3, min_quality=4)

        card = data["highlights"][0]
        assert card["title"] == "My important trace"
        assert card["summary_teaser"] == "Did a big refactor."
        assert card["project"] == "proj-1"
        assert card["source"] == "claude"
        assert card["ai_quality_score"] == 5
        assert card["ai_failure_value_score"] == 5
        assert card["outcome"] == "resolved"
        assert "rationale" in card
        assert card["rationale"].startswith("FV 5/5")

    def test_summary_teaser_truncates(self, index_conn):
        long_summary = "word " * 200  # far more than 200 chars
        _insert(
            index_conn, session_id="s1", quality=5, ended_hours_ago=1,
            summary=long_summary,
        )

        data = get_highlights(index_conn, days=7, top_n=3, min_quality=4)

        teaser = data["highlights"][0]["summary_teaser"]
        assert len(teaser) <= 201  # 200 + trailing ellipsis
        assert teaser.endswith("…")

    def test_rationale_mentions_recency(self, index_conn):
        _insert(index_conn, session_id="yesterday", quality=5, ended_hours_ago=26)
        _insert(index_conn, session_id="week_old", quality=5, ended_hours_ago=24 * 5)

        data = get_highlights(index_conn, days=7, top_n=2, min_quality=4)

        ratls = {h["session_id"]: h["rationale"] for h in data["highlights"]}
        assert "yesterday" in ratls["yesterday"] or "day" in ratls["yesterday"]
        assert "days ago" in ratls["week_old"]


class TestEndpointResponseShape:
    def test_response_has_all_envelope_fields(self, index_conn):
        _insert(index_conn, session_id="s1", quality=5, ended_hours_ago=1)

        data = get_highlights(index_conn, days=7, top_n=3, min_quality=4)

        assert set(data.keys()) == {
            "highlights", "window_days", "min_quality", "min_failure_value", "candidate_count"
        }
        assert data["window_days"] == 7
        assert data["min_quality"] == 4
        assert data["min_failure_value"] == 4

    def test_unscored_sessions_excluded(self, index_conn):
        # Session with NULL ai_failure_value_score (not yet scored) must not appear
        # even if end_time is in range.
        _insert(index_conn, session_id="unscored", quality=None, ended_hours_ago=1)
        _insert(index_conn, session_id="scored", quality=5, ended_hours_ago=2)

        data = get_highlights(index_conn, days=7, top_n=3, min_quality=4)

        ids = [h["session_id"] for h in data["highlights"]]
        assert ids == ["scored"]

    def test_high_quality_low_failure_value_excluded(self, index_conn):
        _insert(index_conn, session_id="productive", quality=5, failure_value=2, ended_hours_ago=1)
        _insert(index_conn, session_id="failure", quality=3, failure_value=4, ended_hours_ago=2)

        data = get_highlights(index_conn, days=7, top_n=3, min_quality=4)

        ids = [h["session_id"] for h in data["highlights"]]
        assert ids == ["failure"]
