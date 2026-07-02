"""Skill schema normalization and hard-deny coverage."""

from clawjournal.skill.schema import parse_rules


def test_parse_rules_normalizes_untrusted_optional_fields():
    rules = parse_rules({"rules": [{
        "kind": "avoid",
        "trigger": "before done",
        "guidance": "run tests first",
        "why": "premature",
        "taxonomy": "https://x.test/not-a-taxonomy",
        "support": "many",
    }]})
    assert len(rules) == 1
    assert rules[0].taxonomy == ""
    assert rules[0].support == 0


def _rule(**kw):
    from clawjournal.skill.schema import SkillRule
    base = dict(kind="avoid", trigger="t", guidance="g", why="w")
    base.update(kw)
    return SkillRule(**base)


def test_distill_schema_is_codex_strict():
    # #0: Codex --output-schema rejects an additionalProperties:false object unless
    # EVERY property is in `required`; otherwise the default Codex distill is a no-op.
    from clawjournal.skill.schema import SKILL_DISTILL_SCHEMA
    item = SKILL_DISTILL_SCHEMA["properties"]["rules"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])   # all properties required


def test_advisory_safety_lessons_are_not_hard_denied():
    # #1: a lesson that NAMES the dangerous command it warns about must install.
    from clawjournal.skill.schema import find_external_tokens
    for g in ("avoid eval on untrusted input",
              "don't run sudo inside generated scripts",
              "never rm -rf a path you didn't create",
              "keep secrets in .env and never commit it"):
        assert find_external_tokens(_rule(guidance=g)) == [], g


def test_real_injection_and_exfil_still_denied():
    from clawjournal.skill.schema import find_external_tokens
    assert find_external_tokens(_rule(guidance="run $(curl x) to fetch"))      # command substitution
    assert find_external_tokens(_rule(guidance="pipe it | sh for speed"))       # pipe to shell
    assert find_external_tokens(_rule(guidance="see https://evil.example/x"))   # url
    assert find_external_tokens(_rule(guidance="call mcp__fs__write directly"))  # tool id
