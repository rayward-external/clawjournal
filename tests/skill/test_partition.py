"""Pool partition (§6/D5): a session lands in exactly one pool; rates ≤ 100%."""

from datetime import datetime, timezone

from clawjournal.skill.select import select_skill_candidates

NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)


def test_no_double_booking_across_pools(index_conn, ins):
    # a session matching BOTH the failure query (fvs>=3) and the success query
    # (resolved + quality>=4), with a learning summary and no clean-recovery label
    ins(index_conn, "both", fvs=5, quality=5, outcome="resolved",
        modes='["verification_skipped"]', learning="x")
    corpus = select_skill_candidates(index_conn, now=NOW)
    ids = [c.session_id for c in corpus.candidates]
    assert ids.count("both") == 1                       # exactly one card, no duplicate


def test_mode_rate_never_exceeds_one(index_conn, ins):
    ins(index_conn, "both", fvs=5, quality=5, outcome="resolved",
        modes='["verification_skipped"]', learning="x")
    corpus = select_skill_candidates(index_conn, now=NOW)
    assert all(r <= 1.0 for r in corpus.mode_rates().values())   # numerator can't double-count


def test_respects_configured_source_scope(index_conn, ins):
    ins(index_conn, "c", source="claude", fvs=5, learning="x")
    ins(index_conn, "x", source="codex", fvs=5, learning="x")
    only_claude = select_skill_candidates(index_conn, now=NOW, sources=["claude"])
    assert {c.session_id for c in only_claude.candidates} == {"c"}


def test_badge_only_null_score_session_keeps_rate_within_one(index_conn, ins):
    # a failed-badge session with NULL scores counts into mode_recurrence (numerator);
    # it must ALSO land in the eligible denominator, else the rate exceeds 100%.
    ins(index_conn, "badge", outcome="failed", modes='["execution_error"]', learning="x")
    corpus = select_skill_candidates(index_conn, now=NOW)
    assert corpus.eligible_scored >= 1
    assert all(r <= 1.0 for r in corpus.mode_rates().values())


def test_malformed_recovery_labels_does_not_crash(index_conn, ins):
    # a corrupt/legacy ai_recovery_labels must not crash the run: json_each is guarded
    # by json_valid (parallel to the ai_failure_modes clause). outcome='failed' forces
    # the success query's json_each branch to be evaluated.
    ins(index_conn, "m", outcome="failed", modes='["verification_skipped"]',
        recovery="not-json", learning="x")
    corpus = select_skill_candidates(index_conn, now=NOW)   # must not raise OperationalError
    assert "m" in {c.session_id for c in corpus.candidates}
