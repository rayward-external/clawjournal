"""Merge loop (§9): cap-5, dedupe, replace-weakest, skip-rejected."""

from clawjournal.cli_skill import merge_rules
from clawjournal.skill import store
from clawjournal.skill.schema import MAX_RULES, SkillRule


def _r(g, support=0, kind="avoid"):
    return SkillRule(kind=kind, trigger="t", guidance=g, why="w", support=support)


def test_caps_at_five_by_support():
    merged = merge_rules([], [_r(f"rule {i}", support=i) for i in range(8)], set())
    assert len(merged) == MAX_RULES
    assert [r.support for r in merged] == [7, 6, 5, 4, 3]


def test_dedupe_prefers_higher_support():
    merged = merge_rules([_r("run tests first", support=2)], [_r("run tests first", support=5)], set())
    assert len(merged) == 1 and merged[0].support == 5


def test_rejected_excluded():
    r = _r("bad rule", support=9)
    assert merge_rules([], [r], {store.fingerprint(r)}) == []


def test_replace_weakest():
    existing = [_r(f"old {i}", support=1) for i in range(5)]      # 5 weak already-kept
    merged = merge_rules(existing, [_r("strong new", support=10)], set())
    assert len(merged) == MAX_RULES
    guides = {r.guidance for r in merged}
    assert "strong new" in guides
    assert sum(g.startswith("old ") for g in guides) == 4        # one weak one displaced


def test_preserves_good_bad_mix():
    # 'avoid' rules carry high mode-recurrence support; 'do' rules get support=0.
    # A support-only merge would drop every 'do'; the interleave must keep both (D2).
    avoid = [_r(f"avoid {i}", support=50, kind="avoid") for i in range(5)]
    do = [_r("do X", support=0, kind="do"), _r("do Y", support=0, kind="do")]
    merged = merge_rules([], avoid + do, set())
    kinds = [r.kind for r in merged]
    assert len(merged) == MAX_RULES
    assert kinds.count("do") >= 1 and kinds.count("avoid") >= 1   # both kinds survive
    assert kinds == ["avoid", "do", "avoid", "do", "avoid"]       # interleaved
