"""Proper rule names (titles): render heading, parse fallback, store round-trip."""

from clawjournal.skill import render, store
from clawjournal.skill.schema import SkillRule, parse_rules

META = {"generated_at": "2026-07-01", "window_days": 3650, "sources": 5}


def _rule(**kw):
    base = dict(kind="avoid", trigger="before merge", guidance="don't skip the migration step",
               why="grounded")
    base.update(kw)
    return SkillRule(**base)


def test_title_is_the_heading_guidance_moves_to_body():
    md = render.render_skill_md([_rule(title="Deploy migration before code")], META)
    assert "### Deploy migration before code" in md          # short name is the heading
    assert "- **Rule:** don't skip the migration step" in md  # full rule kept in body
    assert "### don't skip the migration step" not in md      # sentence is no longer a heading


def test_untitled_rule_falls_back_to_guidance_without_duplicate_rule_line():
    # guidance <=4 words: the derived heading equals it, so no redundant Rule line
    md = render.render_skill_md([_rule(title="", guidance="run smoke tests")], META)
    assert "### run smoke tests" in md
    assert "- **Rule:** run smoke tests" not in md    # no redundant echo of the heading


def test_untitled_long_guidance_keeps_full_text_in_body():
    # guidance >4 words: heading is truncated, full rule preserved in the Rule line
    md = render.render_skill_md([_rule(title="", guidance="reset local state before each run")], META)
    assert "### reset local state before" in md              # 4-word heading
    assert "- **Rule:** reset local state before each run" in md  # full guidance not lost


def test_parse_rules_reads_title_and_falls_back():
    rules = parse_rules({"rules": [
        {"kind": "do", "title": "Verify patch applied", "trigger": "t",
         "guidance": "re-read the file after patching", "why": "w"},
        {"kind": "avoid", "trigger": "t",  # no title -> derived from guidance
         "guidance": "don't trust an unverified diff before building on it", "why": "w"},
    ]})
    assert rules[0].title == "Verify patch applied"
    assert rules[1].title == "don't trust an unverified"  # first 4 words


def test_store_round_trips_title(index_conn):
    r = _rule(title="Deploy migration before code", support=3)
    store.mark_installed(index_conn, [r])
    kept = store.load_kept(index_conn)
    assert kept[0].title == "Deploy migration before code"
    assert kept[0].display_title() == "Deploy migration before code"


def test_fingerprint_ignores_title(index_conn):
    # renaming a rule must not change its identity (dedup/reject stay stable)
    a = _rule(title="Old name")
    b = _rule(title="A completely different name")
    assert store.fingerprint(a) == store.fingerprint(b)
