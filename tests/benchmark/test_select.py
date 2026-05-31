"""Tests for weekly failure-session selection."""

from datetime import datetime, timezone

from clawjournal.benchmark.select import select_week_failures

NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)


def _ins(conn, sid, *, source="codex", start_time="2026-05-28T00:00:00+00:00",
         review_status="new", fvs=None, modes=None, learning=None, reason=None):
    conn.execute(
        "INSERT INTO sessions (session_id, project, source, indexed_at, start_time, "
        "review_status, ai_failure_value_score, ai_failure_modes, ai_learning_summary, "
        "ai_score_reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, "proj", source, "2026-05-28T00:00:00+00:00", start_time, review_status,
         fvs, modes, learning, reason),
    )
    conn.commit()


class TestSelection:
    def test_includes_failure_signal_excludes_clean(self, index_conn):
        _ins(index_conn, "high-fvs", fvs=5, learning="agent fabricated a config value")
        _ins(index_conn, "modes-only", fvs=2, modes='["wrong_assumption"]')
        _ins(index_conn, "clean", fvs=1, modes="[]")          # excluded: no signal
        _ins(index_conn, "clean-null")                          # excluded: nothing
        result = select_week_failures(index_conn, now=NOW)
        assert set(result.session_ids) == {"high-fvs", "modes-only"}

    def test_excludes_out_of_window(self, index_conn):
        _ins(index_conn, "recent", fvs=4, start_time="2026-05-28T00:00:00+00:00")
        _ins(index_conn, "old", fvs=4, start_time="2026-05-10T00:00:00+00:00")
        result = select_week_failures(index_conn, now=NOW, window_days=7)
        assert result.session_ids == ["recent"]
        assert result.window_start == "2026-05-24T12:00:00+00:00"
        assert result.window_end == "2026-05-31T12:00:00+00:00"

    def test_excludes_segmented_parents(self, index_conn):
        _ins(index_conn, "plain", fvs=4)
        _ins(index_conn, "parent", fvs=4, review_status="segmented")
        assert select_week_failures(index_conn, now=NOW).session_ids == ["plain"]

    def test_source_scope_default_and_override(self, index_conn):
        _ins(index_conn, "codex-row", source="codex", fvs=4)
        _ins(index_conn, "gemini-row", source="gemini", fvs=4)
        # default scope excludes gemini
        assert select_week_failures(index_conn, now=NOW).session_ids == ["codex-row"]
        # explicit None scope includes all sources
        ids = set(select_week_failures(index_conn, now=NOW, sources=None).session_ids)
        assert ids == {"codex-row", "gemini-row"}

    def test_orders_by_failure_value_desc(self, index_conn):
        _ins(index_conn, "low", fvs=3)
        _ins(index_conn, "high", fvs=5)
        _ins(index_conn, "mid", fvs=4)
        assert select_week_failures(index_conn, now=NOW).session_ids == ["high", "mid", "low"]

    def test_cap_and_dropped_for_cost(self, index_conn):
        for i in range(5):
            _ins(index_conn, f"s{i}", fvs=5)
        result = select_week_failures(index_conn, now=NOW, cap=3)
        assert len(result.candidates) == 3
        assert result.total_candidates == 5
        assert result.dropped_for_cost == 2

    def test_whitespace_empty_modes_not_selected(self, index_conn):
        # `NOT IN ('','[]')` would let '[ ]' / '[\n]' through; json_array_length won't
        _ins(index_conn, "real", fvs=2, modes='["x"]')
        _ins(index_conn, "ws-empty", fvs=2, modes='[ ]')
        _ins(index_conn, "newline-empty", fvs=2, modes='[\n]')
        assert select_week_failures(index_conn, now=NOW).session_ids == ["real"]

    def test_has_trace_evidence_flag(self, index_conn):
        _ins(index_conn, "evidenced", fvs=4, learning="concrete lesson here")
        _ins(index_conn, "bare-score", fvs=4)  # score only, no modes/learning/reason
        result = select_week_failures(index_conn, now=NOW)
        by_id = {c.session_id: c for c in result.candidates}
        assert by_id["evidenced"].has_trace_evidence is True
        assert by_id["bare-score"].has_trace_evidence is False
