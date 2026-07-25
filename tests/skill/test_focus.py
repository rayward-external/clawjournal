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
    kind: str = "avoid",
    failure_modes: list[str] | None = None,
    recovery_labels: list[str] | None = None,
) -> SkillCandidate:
    return SkillCandidate(
        session_id=sid,
        project=project,
        source=source,
        kind=kind,
        failure_modes=(
            ["verification_skipped"] if failure_modes is None else failure_modes
        ),
        recovery_labels=[] if recovery_labels is None else recovery_labels,
        start_time=f"2026-05-{day:02d}T12:00:00+00:00",
    )


def _corpus(
    candidates: list[SkillCandidate],
    *,
    eligible: list[str] | None = None,
    successes: list[SkillCandidate] | None = None,
) -> SkillCorpus:
    successes = successes or []
    return SkillCorpus(
        window_start="2026-05-20",
        window_end="2026-05-31",
        failures=candidates,
        successes=successes,
        total_failures=len(candidates),
        total_successes=len(successes),
        eligible_session_ids=(
            eligible
            if eligible is not None
            else [candidate.session_id for candidate in candidates + successes]
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


def test_recovered_failure_sessions_count_as_direct_evidence():
    """A cleanly-recovered failure lands in the "do" pool but still witnessed the
    failure mode, so an avoid rule may cite it."""
    corpus = _corpus([], successes=[
        _candidate("s1", "alpha", 27, kind="do", recovery_labels=["self_recovered"]),
        _candidate("s2", "alpha", 28, kind="do",
                   recovery_labels=["user_corrected_recovery"]),
        _candidate("s3", "beta", 28, kind="do", recovery_labels=["self_recovered"]),
    ])
    rule = _rule()

    focus = select_focus(active_rules=[rule], current_rules=[rule], corpus=corpus)

    assert focus is not None
    assert (focus.session_count, focus.day_count, focus.project_count) == (3, 2, 2)


def test_production_corpus_shape_synthetic_avoid_plus_real_do_still_qualifies():
    """Regression for the real 19-candidate corpus that abstained: every
    non-synthetic candidate was kind="do" and every kind="avoid" one was a
    synthetic aggregate. The real sessions must still carry the spotlight."""
    synthetic = [
        SkillCandidate(session_id="env-signature-0", project="gamma", source="codex",
                       kind="avoid", support_count=27),
        SkillCandidate(session_id="human-rejection", project="delta", source="claude",
                       kind="avoid", support_count=31),
    ]
    real = [
        _candidate("s1", "clawjournal", 23, kind="do",
                   recovery_labels=["self_recovered"]),
        _candidate("s2", "clawjournal-share", 28, kind="do",
                   recovery_labels=["user_corrected_recovery"]),
        _candidate("s3", "outputs", 31, kind="do",
                   recovery_labels=["self_recovered"]),
    ]
    corpus = _corpus(synthetic, successes=real, eligible=["s1", "s2", "s3"])
    rule = _rule()
    rule.evidence_session_ids = ["case-01", "case-03", "case-04", "case-05"]

    focus = select_focus(active_rules=[rule], current_rules=[rule], corpus=corpus)

    assert focus is not None
    # case-01 is the synthetic aggregate and must not be counted.
    assert (focus.session_count, focus.day_count, focus.project_count) == (3, 3, 3)


@pytest.mark.parametrize(
    ("failure_modes", "recovery_labels"),
    [
        ([], []),
        ([], ["self_recovered"]),
        (["verification_skipped"], []),
        (["execution_error"], ["self_recovered"]),
    ],
    ids=[
        "pure-success",
        "recovery-without-mode",
        "matching-mode-without-recovery",
        "recovered-other-mode",
    ],
)
def test_unrelated_do_sessions_do_not_count_as_direct_avoid_evidence(
    failure_modes,
    recovery_labels,
):
    corpus = _corpus([], successes=[
        _candidate("s1", "alpha", 27, kind="do",
                   failure_modes=failure_modes, recovery_labels=recovery_labels),
        _candidate("s2", "alpha", 28, kind="do",
                   failure_modes=failure_modes, recovery_labels=recovery_labels),
        _candidate("s3", "beta", 28, kind="do",
                   failure_modes=failure_modes, recovery_labels=recovery_labels),
    ])
    rule = _rule()

    assert select_focus(active_rules=[rule], current_rules=[rule], corpus=corpus) is None


@pytest.mark.parametrize("taxonomy", ["", "not_a_failure_mode"])
def test_rule_requires_a_valid_taxonomy_for_direct_evidence(taxonomy):
    corpus = _corpus([
        _candidate("s1", "alpha", 27),
        _candidate("s2", "alpha", 28),
        _candidate("s3", "beta", 28),
    ])
    rule = _rule()
    rule.taxonomy = taxonomy

    assert select_focus(active_rules=[rule], current_rules=[rule], corpus=corpus) is None


def test_unrelated_citations_are_excluded_from_reported_evidence_count():
    relevant = [
        _candidate("s1", "alpha", 27),
        _candidate("s2", "alpha", 28),
        _candidate("s3", "beta", 28),
    ]
    unrelated = _candidate(
        "s4", "gamma", 29, kind="do", failure_modes=[], recovery_labels=[]
    )
    corpus = _corpus(relevant, successes=[unrelated])
    rule = _rule()
    rule.evidence_session_ids.append("case-04")

    focus = select_focus(active_rules=[rule], current_rules=[rule], corpus=corpus)

    assert focus is not None
    assert (focus.session_count, focus.project_count) == (3, 2)


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


@pytest.mark.parametrize("field", ["title", "trigger", "guidance", "why"])
def test_personal_attribution_is_rejected_in_every_displayed_field(field):
    corpus = _corpus([
        _candidate("s1", "alpha", 27),
        _candidate("s2", "alpha", 28),
        _candidate("s3", "beta", 28),
    ])
    rule = _rule()
    setattr(rule, field, "The developer’s repeated carelessness caused rework")

    assert select_focus(active_rules=[rule], current_rules=[rule], corpus=corpus) is None


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("why", "The engineer was unreliable and delayed the handoff"),
        ("title", "Procrastination"),
        ("trigger", "when you are careless about final validation"),
        ("guidance", "because you are lazy, re-run final verification"),
        ("guidance", "stop behaving recklessly"),
        ("title", "Developer Carelessness"),
        ("guidance", "The developer shows repeated carelessness; rerun verification"),
        ("trigger", "when carelessness by the developer affects validation"),
        ("guidance", "Their carelessness requires another verification run"),
        ("guidance", "The developer showed repeated carelessness; rerun verification"),
        ("why", "The engineer acted carelessly and delayed the handoff"),
        ("trigger", "when the user displayed persistent overconfidence"),
        ("guidance", "The developer has been careless about validation"),
        ("why", "The engineer became careless and delayed the handoff"),
        ("why", "The developer’s pattern of carelessness caused rework"),
        ("guidance", "The developer demonstrated a pattern of carelessness"),
        ("why", "The engineer was extremely careless"),
        ("trigger", "The developer repeatedly procrastinated before final verification"),
    ],
)
def test_personal_trait_morphology_does_not_bypass_safeguard(field, text):
    corpus = _corpus([
        _candidate("s1", "alpha", 27),
        _candidate("s2", "alpha", 28),
        _candidate("s3", "beta", 28),
    ])
    rule = _rule()
    setattr(rule, field, text)

    assert select_focus(active_rules=[rule], current_rules=[rule], corpus=corpus) is None


def test_technical_adjectives_and_agent_facing_triggers_remain_eligible():
    corpus = _corpus([
        _candidate("s1", "alpha", 27),
        _candidate("s2", "alpha", 28),
        _candidate("s3", "beta", 28),
    ])
    rule = _rule(
        why="an unreliable integration test masked the regression and caused rework"
    )
    rule.trigger = "when the developer asks for a merge verdict"

    assert select_focus(active_rules=[rule], current_rules=[rule], corpus=corpus) is not None


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("trigger", "when the developer is fixing an unreliable test"),
        ("trigger", "when you are debugging an unreliable integration test"),
        ("guidance", "the coding agent is debugging an unreliable test"),
        ("guidance", "repair the developer’s unreliable test before trusting it"),
        ("why", "A user-facing report showed the wrong final status"),
        ("why", "The stale status forced developers to repeat the handoff"),
        ("trigger", "when the developer addresses CI unreliability before merging"),
        ("guidance", "the developer fixed the integration test’s unreliability"),
        ("guidance", "the coding agent corrected flaky-test unreliability"),
        ("trigger", "when their integration test shows persistent unreliability"),
    ],
)
def test_personal_safeguard_allows_technical_and_consequence_wording(field, text):
    corpus = _corpus([
        _candidate("s1", "alpha", 27),
        _candidate("s2", "alpha", 28),
        _candidate("s3", "beta", 28),
    ])
    rule = _rule()
    setattr(rule, field, text)

    assert select_focus(active_rules=[rule], current_rules=[rule], corpus=corpus) is not None


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


def test_personal_label_in_trigger_is_not_focus_eligible():
    corpus = _corpus([
        _candidate("s1", "alpha", 27),
        _candidate("s2", "alpha", 28),
        _candidate("s3", "beta", 28),
    ])
    rule = _rule()
    rule.trigger = "when the user is careless about verifying results"
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
