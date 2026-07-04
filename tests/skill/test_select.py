"""Candidate selection: failures (avoid) + successes/recoveries (do)."""

from datetime import datetime, timezone

from clawjournal.skill.select import select_skill_candidates

NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)


def test_splits_avoid_and_do(index_conn, ins):
    ins(index_conn, "fail", fvs=5, modes='["verification_skipped"]',
        learning="declared done before running tests")
    ins(index_conn, "win", outcome="resolved", quality=5,
        learning="wrote a failing repro first, then fixed")
    ins(index_conn, "recovered", modes='["execution_error"]', recovery='["self_recovered"]',
        learning="re-read the traceback and reproduced before editing")
    corpus = select_skill_candidates(index_conn, now=NOW)
    assert {c.session_id for c in corpus.failures} == {"fail"}
    assert {c.session_id for c in corpus.successes} == {"win", "recovered"}


def test_drops_unevidenced_failures(index_conn, ins):
    ins(index_conn, "bare", fvs=4)                      # no learning/reason/modes -> dropped
    ins(index_conn, "evidenced", fvs=4, reason="agent fabricated output")
    corpus = select_skill_candidates(index_conn, now=NOW)
    assert {c.session_id for c in corpus.failures} == {"evidenced"}


def test_drops_low_impact_mode_only_failures(index_conn, ins):
    ins(index_conn, "low", fvs=1, modes='["verification_skipped"]',
        outcome="resolved", learning="minor wobble")
    ins(index_conn, "failed", fvs=2, modes='["execution_error"]',
        outcome="failed", learning="task did not finish")
    corpus = select_skill_candidates(index_conn, now=NOW)
    assert {c.session_id for c in corpus.failures} == {"failed"}


def test_mode_recurrence_counts_all(index_conn, ins):
    for i in range(3):
        ins(index_conn, f"v{i}", fvs=4, modes='["verification_skipped"]', learning="x")
    ins(index_conn, "other", fvs=4, modes='["reasoning_fabrication"]', learning="y")
    corpus = select_skill_candidates(index_conn, now=NOW)
    assert corpus.mode_recurrence["verification_skipped"] == 3
    assert corpus.mode_recurrence["reasoning_fabrication"] == 1


def test_excludes_bad_recovery_from_do(index_conn, ins):
    ins(index_conn, "blocked", modes='["execution_error"]', recovery='["blocked"]', learning="z")
    corpus = select_skill_candidates(index_conn, now=NOW)
    assert not corpus.successes


def test_selection_caps_to_ranked_top_pool(index_conn, ins):
    # explicit pool_cap so this exercises the cap mechanism regardless of the default
    for i, score in enumerate([3, 3, 3, 4, 5, 5]):
        ins(index_conn, f"f{i}", fvs=score, modes='["verification_skipped"]',
            learning=f"failure {i}")
    corpus = select_skill_candidates(index_conn, now=NOW, pool_cap=5)
    selected = {c.session_id for c in corpus.candidates}
    assert len(corpus.candidates) == 5
    assert corpus.total_failures == 6
    assert {"f3", "f4", "f5"}.issubset(selected)
    assert len({"f0", "f1", "f2"} - selected) == 1


def test_hold_state_gate_excludes_pending(index_conn, ins):
    ins(index_conn, "ok", fvs=5, learning="a", hold_state="auto_redacted")
    ins(index_conn, "pending", fvs=5, learning="b", hold_state="pending_review")
    corpus = select_skill_candidates(index_conn, now=NOW)
    assert {c.session_id for c in corpus.failures} == {"ok"}


def test_hold_state_gate_excludes_pending_from_rates(index_conn, ins):
    ins(index_conn, "ok", fvs=5, modes='["verification_skipped"]',
        learning="a", hold_state="auto_redacted")
    ins(index_conn, "pending", fvs=5, modes='["safety_security"]',
        learning="b", hold_state="pending_review")
    corpus = select_skill_candidates(index_conn, now=NOW)
    assert corpus.mode_recurrence == {"verification_skipped": 1}
    assert corpus.eligible_scored == 1


def test_window_and_source_scope(index_conn, ins):
    ins(index_conn, "recent", fvs=4, learning="x", start_time="2026-05-28T00:00:00+00:00")
    ins(index_conn, "old", fvs=4, learning="x", start_time="2026-05-01T00:00:00+00:00")
    ins(index_conn, "gem", fvs=4, learning="x", source="gemini")
    corpus = select_skill_candidates(index_conn, now=NOW, window_days=7)
    assert {c.session_id for c in corpus.failures} == {"recent"}  # old out-of-window, gemini out-of-scope


def test_corrections_boost_rank_into_pool(index_conn, ins):
    # a captured user-correction is direct teachable evidence: with equal support, the
    # corrected session must win the pool slot over a higher-severity summary-only peer.
    from clawjournal.skill.turns import TurnExcerpt
    ins(index_conn, "summary_only", fvs=5, modes='["verification_skipped"]', learning="a")
    ins(index_conn, "corrected", fvs=3, modes='["reasoning_fabrication"]', learning="b")
    excerpts = {"corrected": [TurnExcerpt(before="done!", correction="no, wrong", after="fixed"),
                              TurnExcerpt(before="done!!", correction="still wrong", after="ok")]}
    corpus = select_skill_candidates(
        index_conn, now=NOW, pool_cap=1,
        excerpt_loader=lambda sid: excerpts.get(sid, []))
    (kept,) = corpus.candidates
    assert kept.session_id == "corrected"          # 2.0x grounding beats severity 2.0 vs 1.6
    assert len(kept.pivotal_excerpts) == 2         # excerpts ride along to the distiller


def test_broken_excerpt_loader_degrades_not_fatal(index_conn, ins):
    ins(index_conn, "f1", fvs=4, modes='["verification_skipped"]', learning="a")

    def boom(sid):
        raise RuntimeError("loader broke")

    corpus = select_skill_candidates(index_conn, now=NOW, excerpt_loader=boom)
    (kept,) = corpus.failures
    assert kept.session_id == "f1" and kept.pivotal_excerpts == []
