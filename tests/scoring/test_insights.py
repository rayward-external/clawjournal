"""Tests for clawjournal.scoring.insights."""

from datetime import datetime, timedelta, timezone

import pytest

from clawjournal.workbench.index import open_index, upsert_sessions
from clawjournal.scoring.insights import collect_advisor_stats, generate_recommendations


@pytest.fixture
def index_conn(tmp_path, monkeypatch):
    """Open an index DB in a temp directory."""
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    conn = open_index()
    yield conn
    conn.close()


def _make_session(session_id: str = "sess-1") -> dict:
    now = datetime.now(timezone.utc)
    later = now + timedelta(minutes=10)
    return {
        "session_id": session_id,
        "project": "test-project",
        "source": "claude",
        "model": "claude-sonnet-4",
        "start_time": now.isoformat(),
        "end_time": later.isoformat(),
        "git_branch": "main",
        "messages": [
            {"role": "user", "content": "Document the config changes", "tool_uses": []},
            {"role": "assistant", "content": "Done.", "tool_uses": []},
        ],
        "stats": {
            "user_messages": 1,
            "assistant_messages": 1,
            "tool_uses": 0,
            "input_tokens": 400_000,
            "output_tokens": 100_000,
        },
    }


class TestCollectAdvisorStats:
    @pytest.mark.parametrize("task_type", ["documentation", "configuration"])
    def test_model_downgrade_candidates_include_new_task_labels(self, index_conn, task_type):
        upsert_sessions(index_conn, [_make_session()])
        index_conn.execute(
            "UPDATE sessions SET ai_quality_score = ?, ai_task_type = ? WHERE session_id = ?",
            (2, task_type, "sess-1"),
        )
        index_conn.commit()

        stats = collect_advisor_stats(index_conn, days=30)

        assert any(
            candidate["session_id"] == "sess-1"
            for candidate in stats["model_downgrade_candidates"]
        )


class TestCostPerSessionDenominator:
    def test_unpriced_sessions_excluded_from_average(self, index_conn):
        upsert_sessions(index_conn, [_make_session("sess-1"), _make_session("sess-2")])
        index_conn.execute(
            "UPDATE sessions SET estimated_cost_usd = 10.0 WHERE session_id = 'sess-1'")
        index_conn.execute(
            "UPDATE sessions SET estimated_cost_usd = NULL WHERE session_id = 'sess-2'")
        index_conn.commit()

        stats = collect_advisor_stats(index_conn, days=30)
        assert stats["total_sessions"] == 2
        assert stats["priced_sessions"] == 1
        assert stats["unpriced_sessions"] == 1
        assert stats["total_cost_usd"] == pytest.approx(10.0)

        summary = generate_recommendations(stats)["summary_stats"]
        # 10.0 over 1 priced session — NOT 10.0 / 2 total sessions.
        assert summary["cost_per_session"] == pytest.approx(10.0)
        assert summary["unpriced_sessions"] == 1

    def test_unpriced_models_do_not_win_most_efficient(self, index_conn):
        upsert_sessions(index_conn, [_make_session("priced"), _make_session("unpriced")])
        index_conn.execute(
            "UPDATE sessions SET model = 'claude-sonnet-4', ai_quality_score = 4, "
            "estimated_cost_usd = 5.0 WHERE session_id = 'priced'")
        index_conn.execute(
            "UPDATE sessions SET model = 'unknown-future-model', ai_quality_score = 5, "
            "estimated_cost_usd = NULL WHERE session_id = 'unpriced'")
        index_conn.commit()

        summary = generate_recommendations(collect_advisor_stats(index_conn, days=30))["summary_stats"]
        assert summary["most_efficient_model"] == "claude-sonnet-4"
        # Quality is independent of pricing, so the unpriced model can still win
        # the highest-quality field when its score is higher.
        assert summary["highest_quality_model"] == "unknown-future-model"


class TestModelDowngradeSavings:
    def _make_candidate(self, index_conn, cost: float) -> None:
        upsert_sessions(index_conn, [_make_session("sess-1")])
        index_conn.execute(
            "UPDATE sessions SET ai_quality_score = 2, ai_task_type = 'documentation', "
            "estimated_cost_usd = ? WHERE session_id = 'sess-1'",
            (cost,),
        )
        index_conn.commit()

    def test_savings_uses_real_downgrade_ratio(self, index_conn, monkeypatch):
        monkeypatch.setattr(
            "clawjournal.scoring.insights.downgrade_savings_ratio", lambda m: 0.5)
        self._make_candidate(index_conn, 4.0)

        rec = generate_recommendations(collect_advisor_stats(index_conn, days=30))
        downgrade = [r for r in rec["recommendations"] if r["type"] == "model_downgrade"]
        assert downgrade
        # 4.0 * 0.5 = 2.0 — proves the real delta is used (legacy flat 0.6 -> 2.4).
        assert downgrade[0]["estimated_savings_usd"] == pytest.approx(2.0)

    def test_savings_falls_back_to_flat_factor_when_unpriced(self, index_conn, monkeypatch):
        monkeypatch.setattr(
            "clawjournal.scoring.insights.downgrade_savings_ratio", lambda m: None)
        self._make_candidate(index_conn, 4.0)

        rec = generate_recommendations(collect_advisor_stats(index_conn, days=30))
        downgrade = [r for r in rec["recommendations"] if r["type"] == "model_downgrade"]
        assert downgrade
        # ratio None -> fallback 0.6: 4.0 * 0.6 = 2.4.
        assert downgrade[0]["estimated_savings_usd"] == pytest.approx(2.4)
