"""Merge loop (§9): cap-5, dedupe, replace-weakest, skip-rejected."""

from clawjournal.cli_skill import merge_rules
from clawjournal.skill import store
from clawjournal.skill.schema import MAX_RULES, SkillRule


def _tr(g, *, taxonomy="", support=0, kind="avoid"):
    return SkillRule(kind=kind, trigger="t", guidance=g, why="w", taxonomy=taxonomy, support=support)


def test_semantic_dedup_collapses_paraphrase_prefers_carried():
    # a reworded variant (different fingerprint, same lesson) must NOT install alongside
    # the original and crowd out a distinct rule; the CARRIED original is kept (no churn).
    carried = _tr("never echo secret values in error output; redact or reference them by name",
                  taxonomy="safety_security", support=5)
    fresh_dup = _tr("never echo credential values; redact sensitive fields and refer to secrets by name",
                    taxonomy="safety_security", support=9)
    distinct = _tr("probe environment constraints before running a generated script",
                   taxonomy="execution_error", support=3)
    merged = merge_rules([carried, distinct], [fresh_dup], set())
    guides = [r.guidance for r in merged]
    assert sum(("secret" in g or "credential" in g) for g in guides) == 1        # collapsed to one
    assert any("never echo secret values in error output" in g for g in guides)  # carried kept
    assert any("probe environment constraints" in g for g in guides)             # distinct preserved


def test_semantic_dedup_refreshes_carried_last_seen():
    # when the carried rule wins over a fresh paraphrase, mark it seen-this-run so a
    # lesson the distiller keeps re-teaching (reworded) doesn't decay out.
    carried = _tr("never echo secret values in error output; redact or reference them by name",
                  taxonomy="safety_security", support=5)
    carried.last_seen = "2026-01-01T00:00:00+00:00"   # old -> would decay
    fresh_dup = _tr("never echo credential values; redact sensitive fields and refer to secrets by name",
                    taxonomy="safety_security", support=9)
    merged = merge_rules([carried], [fresh_dup], set())
    kept = [r for r in merged if ("secret" in r.guidance or "credential" in r.guidance)]
    assert len(kept) == 1 and kept[0].last_seen == ""   # refreshed, won't age out


def test_taxonomy_collapse_merges_same_mode_avoid_rules():
    # same failure MODE => same lesson for the 5-rule budget (breadth over depth), even
    # when the two rewrites share almost no vocabulary (word-overlap alone would miss it).
    a = _tr("run the full regression suite before claiming a fix is done",
            taxonomy="verification_skipped", support=5)
    b = _tr("confirm an issue is reproducible on a clean clone before debugging",
            taxonomy="verification_skipped", support=4)
    merged = merge_rules([], [a, b], set())
    assert len(merged) == 1 and merged[0].taxonomy == "verification_skipped"


def test_cross_kind_same_title_collapses():
    # the distiller emits one lesson as BOTH a 'do' and an 'avoid' with the same title;
    # they must collapse to one even though the kinds differ.
    avoid = SkillRule(kind="avoid", trigger="t", guidance="don't trust a green harness as done",
                      why="w", title="Verify Beyond Green Tests", taxonomy="verification_skipped",
                      support=9)
    do = SkillRule(kind="do", trigger="t", guidance="adversarially self-review before finishing",
                   why="w", title="Verify Beyond Green Tests", support=4)
    merged = merge_rules([do], [avoid], set())
    assert sum(r.title == "Verify Beyond Green Tests" for r in merged) == 1


def test_distinct_do_rules_survive():
    # 'do' rules carry no taxonomy; only genuine paraphrases (high overlap) collapse.
    a = _tr("write a failing regression test that reproduces the bug before fixing", kind="do")
    b = _tr("probe environment constraints and shell version before running a script", kind="do")
    merged = merge_rules([], [a, b], set())
    assert len(merged) == 2   # different topics, low overlap -> both kept


def _r(g, support=0, kind="avoid"):
    return SkillRule(kind=kind, trigger="t", guidance=g, why="w", support=support)


def test_caps_at_five_by_support():
    # distinct wording per rule so the paraphrase dedup doesn't collapse them
    merged = merge_rules([], [_r(f"topic{i} action{i} lesson", support=i) for i in range(8)], set())
    assert len(merged) == MAX_RULES
    assert [r.support for r in merged] == [7, 6, 5, 4, 3]


def test_dedupe_prefers_higher_support():
    merged = merge_rules([_r("run tests first", support=2)], [_r("run tests first", support=5)], set())
    assert len(merged) == 1 and merged[0].support == 5


def test_rejected_excluded():
    r = _r("bad rule", support=9)
    assert merge_rules([], [r], {store.fingerprint(r)}) == []


def test_replace_weakest():
    existing = [_r(f"weak{i} old{i} habit", support=1) for i in range(5)]   # 5 distinct weak
    merged = merge_rules(existing, [_r("strong brandnew distinct lesson", support=10)], set())
    assert len(merged) == MAX_RULES
    guides = {r.guidance for r in merged}
    assert "strong brandnew distinct lesson" in guides
    assert sum(g.startswith("weak") for g in guides) == 4        # one weak one displaced


def test_recency_decays_stale_support_below_fresh():
    # #4: a once-frequent but idle rule must decay so a currently-relevant rule can
    # outrank it — without decay, MAX(support) would pin the stale peak on top forever.
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    stale = _r("stale peak", support=50, kind="avoid")
    stale.last_seen = (now - timedelta(days=120)).isoformat()   # ~4 half-lives -> 50/16
    fresh = _r("fresh rule", support=10, kind="avoid")          # last_seen "" -> seen now
    merged = merge_rules([stale], [fresh], set(), now=now)
    assert [r.guidance for r in merged][0] == "fresh rule"      # fresh outranks the stale peak


def test_merge_rules_tolerates_naive_now():
    # a naive `now` must not crash the aware-vs-naive subtraction in _decayed_support.
    from datetime import datetime
    stale = _r("s", support=5)
    stale.last_seen = "2026-01-01T00:00:00+00:00"
    merge_rules([stale], [_r("f", support=1)], set(), now=datetime(2026, 6, 1))  # no raise


def test_preserves_good_bad_mix():
    # 'avoid' rules carry high mode-recurrence support; 'do' rules get support=0.
    # A support-only merge would drop every 'do'; the interleave must keep both (D2).
    avoid = [_r(f"badhabit{i} mistake{i} pitfall", support=50, kind="avoid") for i in range(5)]
    do = [_r("alpha task workflow", support=0, kind="do"), _r("bravo chore routine", support=0, kind="do")]
    merged = merge_rules([], avoid + do, set())
    kinds = [r.kind for r in merged]
    assert len(merged) == MAX_RULES
    assert kinds.count("do") >= 1 and kinds.count("avoid") >= 1   # both kinds survive
    assert kinds == ["avoid", "do", "avoid", "do", "avoid"]       # interleaved
