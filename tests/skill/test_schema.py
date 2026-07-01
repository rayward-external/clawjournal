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
