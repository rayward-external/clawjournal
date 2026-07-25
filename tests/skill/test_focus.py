"""Preview-only weekly focus selection and evidence calibration."""

import pytest

from clawjournal.skill.focus import select_focus
from clawjournal.skill.schema import SkillRule
from clawjournal.skill.select import SkillCandidate, SkillCorpus


def _candidate(
    sid: str,
    project: str,
    day: int,
    *,
    source: str = "codex",
) -> SkillCandidate:
    return SkillCandidate(
        session_id=sid,
        project=project,
        source=source,
        kind="avoid",
        failure_modes=["verification_skipped"],
        start_time=f"2026-05-{day:02d}T12:00:00+00:00",
    )


def _corpus(candidates: list[SkillCandidate], *, eligible: list[str] | None = None) -> SkillCorpus:
    return SkillCorpus(
        window_start="2026-05-20",
        window_end="2026-05-31",
        failures=candidates,
        total_failures=len(candidates),
        eligible_session_ids=(
            eligible if eligible is not None else [candidate.session_id for candidate in candidates]
        ),
    )


def _rule(
    guidance: str = "re-run final verification before reporting the outcome",
    *,
    kind: str = "avoid",
    why: str = "stale intermediate results produced incorrect final status reports",
) -> SkillRule:
    return SkillRule(
        kind=kind,
        title="Recompute Final Badge",
        trigger="before reporting a final task status",
        guidance=guidance,
        why=why,
        taxonomy="verification_skipped" if kind == "avoid" else "",
        evidence_session_ids=["case-01", "case-02", "case-03"],
    )


def test_selects_fresh_surviving_avoid_rule_at_three_by_two_by_two():
    corpus = _corpus([
        _candidate("s1", "alpha", 27),
        _candidate("s2", "alpha", 28),
        _candidate("s3", "beta", 28),
    ])
    rule = _rule()

    focus = select_focus(active_rules=[rule], current_rules=[rule], corpus=corpus)

    assert focus is not None
    assert focus.rule is rule
    assert (focus.session_count, focus.day_count, focus.project_count) == (3, 2, 2)


@pytest.mark.parametrize(
    "candidates",
    [
        [_candidate("s1", "alpha", 27), _candidate("s2", "beta", 28)],
        [
            _candidate("s1", "alpha", 27),
            _candidate("s2", "alpha", 27),
            _candidate("s3", "beta", 27),
        ],
        [
            _candidate("s1", "Alpha", 27),
            _candidate("s2", "alpha", 28),
            _candidate("s3", "ALPHA", 29),
        ],
    ],
    ids=["two-sessions", "one-day", "one-project"],
)
def test_abstains_when_any_evidence_breadth_threshold_is_missing(candidates):
    rule = _rule()
    assert select_focus(
        active_rules=[rule],
        current_rules=[rule],
        corpus=_corpus(candidates),
    ) is None


def test_synthetic_aggregate_does_not_count_as_direct_session_evidence():
    candidates = [
        _candidate("s1", "alpha", 27),
        _candidate("s2", "beta", 28),
        SkillCandidate(
            session_id="env-signature-0",
            project="gamma",
            source="codex",
            kind="avoid",
            start_time="2026-05-29T12:00:00+00:00",
            support_count=27,
        ),
    ]
    rule = _rule()
    assert select_focus(
        active_rules=[rule],
        current_rules=[rule],
        corpus=_corpus(candidates, eligible=["s1", "s2"]),
    ) is None


def test_real_and_synthetic_id_collision_makes_the_alias_ineligible():
    candidates = [
        _candidate("s1", "alpha", 27),
        _candidate("s2", "beta", 28),
        _candidate("env-signature-0", "gamma", 29),
        SkillCandidate(
            session_id="env-signature-0",
            project="delta",
            source="codex",
            kind="avoid",
            start_time="2026-05-30T12:00:00+00:00",
            support_count=27,
        ),
    ]
    corpus = _corpus(candidates, eligible=["s1", "s2", "env-signature-0"])
    rule = _rule()
    rule.evidence_session_ids = ["case-01", "case-02", "case-04"]

    assert select_focus(
        active_rules=[rule],
        current_rules=[rule],
        corpus=corpus,
    ) is None


def test_source_prefixes_do_not_make_one_repo_count_as_multiple_projects():
    candidates = [
        _candidate("s1", "codex:clawjournal", 27, source="codex"),
        _candidate("s2", "claude:clawjournal", 28, source="claude"),
        _candidate("s3", "codex:clawjournal", 29, source="codex"),
    ]
    rule = _rule()
    assert select_focus(
        active_rules=[rule],
        current_rules=[rule],
        corpus=_corpus(candidates),
    ) is None


def test_carried_rule_cannot_remap_stale_case_aliases_to_current_sessions():
    corpus = _corpus([
        _candidate("new-1", "alpha", 27),
        _candidate("new-2", "alpha", 28),
        _candidate("new-3", "beta", 28),
    ])
    carried = _rule(guidance="a carried lesson with stale aliases")
    fresh = _rule(guidance="a different current-run lesson")

    assert select_focus(
        active_rules=[carried],
        current_rules=[fresh],
        corpus=corpus,
    ) is None


def test_rule_must_survive_final_active_set_and_be_an_avoid_rule():
    corpus = _corpus([
        _candidate("s1", "alpha", 27),
        _candidate("s2", "alpha", 28),
        _candidate("s3", "beta", 28),
    ])
    avoid = _rule()
    do = _rule(guidance="repeat a successful technique", kind="do")

    assert select_focus(active_rules=[], current_rules=[avoid], corpus=corpus) is None
    assert select_focus(active_rules=[do], current_rules=[do], corpus=corpus) is None


def test_direct_breadth_wins_and_active_order_breaks_ties():
    candidates = [
        _candidate("s1", "alpha", 26),
        _candidate("s2", "alpha", 27),
        _candidate("s3", "beta", 28),
        _candidate("s4", "gamma", 29),
    ]
    corpus = _corpus(candidates)
    three = _rule(guidance="three-case lesson")
    four = _rule(guidance="four-case lesson")
    four.evidence_session_ids.append("case-04")
    tied = _rule(guidance="another four-case lesson")
    tied.evidence_session_ids.append("case-04")

    focus = select_focus(
        active_rules=[tied, three, four],
        current_rules=[three, four, tied],
        corpus=corpus,
    )

    assert focus is not None
    assert focus.rule is tied
    assert focus.session_count == 4


def test_work_performance_diagnosis_is_not_focus_eligible():
    corpus = _corpus([
        _candidate("s1", "alpha", 27),
        _candidate("s2", "alpha", 28),
        _candidate("s3", "beta", 28),
    ])
    rule = _rule(why="this limits your performance at work")
    assert select_focus(active_rules=[rule], current_rules=[rule], corpus=corpus) is None


def test_personal_character_diagnosis_is_not_focus_eligible():
    corpus = _corpus([
        _candidate("s1", "alpha", 27),
        _candidate("s2", "alpha", 28),
        _candidate("s3", "beta", 28),
    ])
    rule = _rule(why="the pattern shows that you are careless and unreliable")
    assert select_focus(active_rules=[rule], current_rules=[rule], corpus=corpus) is None


@pytest.mark.parametrize("title", ["User Is Careless", "Careless", "Careless User"])
def test_personal_character_title_is_not_focus_eligible(title):
    corpus = _corpus([
        _candidate("s1", "alpha", 27),
        _candidate("s2", "alpha", 28),
        _candidate("s3", "beta", 28),
    ])
    rule = _rule()
    rule.title = title
    assert select_focus(active_rules=[rule], current_rules=[rule], corpus=corpus) is None


def test_personal_character_directive_is_not_focus_eligible():
    corpus = _corpus([
        _candidate("s1", "alpha", 27),
        _candidate("s2", "alpha", 28),
        _candidate("s3", "beta", 28),
    ])
    rule = _rule(guidance="stop being careless")
    assert select_focus(active_rules=[rule], current_rules=[rule], corpus=corpus) is None


def test_duplicate_fingerprint_uses_last_fresh_rules_wording_and_evidence():
    corpus = _corpus([
        _candidate("s1", "alpha", 27),
        _candidate("s2", "alpha", 28),
        _candidate("s3", "beta", 28),
    ])
    first = _rule(why="first cost")
    first.evidence_session_ids = ["case-01"]
    last = _rule(why="last cost")

    focus = select_focus(
        active_rules=[last],
        current_rules=[first, last],
        corpus=corpus,
    )

    assert focus is not None
    assert focus.rule is last
    assert focus.rule.why == "last cost"
    assert focus.session_count == 3


def test_duplicate_fingerprint_cannot_borrow_safe_evidence_for_unsafe_last_rule():
    corpus = _corpus([
        _candidate("s1", "alpha", 27),
        _candidate("s2", "alpha", 28),
        _candidate("s3", "beta", 28),
    ])
    first = _rule()
    last = _rule(why="this proves poor performance at work")
    last.evidence_session_ids = ["case-01"]

    assert select_focus(
        active_rules=[last],
        current_rules=[first, last],
        corpus=corpus,
    ) is None
