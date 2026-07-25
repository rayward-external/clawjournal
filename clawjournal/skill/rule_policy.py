"""Deterministic policy checks shared by preview, rendering, and installation."""

from __future__ import annotations

import re

from .schema import SkillRule

UNSUPPORTED_PERSONAL_CLAIM = "unsupported_personal_claim"

_UNSUPPORTED_PERSONAL_CLAIM_RE = re.compile(
    r"\b(?:work performance|performance at work|job performance|"
    r"effectiveness at work|workplace effectiveness|personality|"
    r"character flaw|work ethic|bad habit)\b",
    re.IGNORECASE,
)
_SECOND_PERSON_WHY_RE = re.compile(r"\b(?:you|your|yours|yourself)\b", re.IGNORECASE)
_HUMAN_ROLE = (
    r"(?:users?|developers?|engineers?|employees?|persons?|people|"
    r"programmers?|workers?|coders?|authors?)"
)
_PERSON_ACTOR = rf"(?:you|{_HUMAN_ROLE}|(?:coding\s+)?agents?)"
_PERSON_ACTOR_NOUN = rf"(?:{_HUMAN_ROLE}|(?:coding\s+)?agents?)"
_PERSON_TRAIT_ADJECTIVE = (
    r"(?:careless|lazy|unreliable|incompetent|undisciplined|disorganized|"
    r"reckless|impulsive|impatient|inattentive|overconfident|negligent|"
    r"sloppy|irresponsible|complacent)"
)
_PERSON_TRAIT_NOUN = (
    r"(?:carelessness|laziness|unreliability|incompetence|indiscipline|"
    r"disorganization|recklessness|impulsivity|impatience|inattention|"
    r"overconfidence|negligence|sloppiness|irresponsibility|complacency|"
    r"procrastination)"
)
_PERSON_TRAIT_ADVERB = (
    r"(?:carelessly|lazily|unreliably|incompetently|recklessly|impulsively|"
    r"impatiently|inattentively|negligently|sloppily|irresponsibly|complacently)"
)
_PERSON_TRAIT = (
    rf"(?:{_PERSON_TRAIT_ADJECTIVE}|{_PERSON_TRAIT_NOUN}|"
    rf"{_PERSON_TRAIT_ADVERB}|procrastinat(?:e|es|ed|ing))"
)
_PERSON_MODIFIER = (
    r"(?:(?:very|too|so|quite|rather|repeated|repeatedly|consistent|consistently|"
    r"chronic|chronically|apparent|apparently|seeming|seemingly|clear|clearly|"
    r"persistent|persistently|habitual|habitually|\w+ly)\s+){0,2}"
)
_PERSONAL_COPULA_RE = re.compile(
    rf"\b{_PERSON_ACTOR}\b\s+(?:(?:is|are|was|were|seems?|appears?|became|"
    rf"remained|has\s+been|have\s+been|had\s+been)\s+"
    rf"{_PERSON_MODIFIER}(?:{_PERSON_TRAIT_ADJECTIVE}|{_PERSON_TRAIT_NOUN})|"
    rf"(?:acts?|acted|behaves?|behaved)\s+"
    rf"{_PERSON_MODIFIER}{_PERSON_TRAIT_ADVERB})\b",
    re.IGNORECASE,
)
_PERSON_POSSESSIVE = (
    r"(?:your|(?:user|developer|engineer|employee|person|programmer|worker|"
    r"coder|author|agent)(?:['’]s|s['’]))"
)
_PERSON_TRAIT_NOUN_PHRASE = (
    rf"(?:a\s+)?{_PERSON_MODIFIER}"
    rf"(?:(?:pattern|habit|history)\s+of\s+)?{_PERSON_TRAIT_NOUN}"
)
_PERSONAL_POSSESSIVE_RE = re.compile(
    rf"\b{_PERSON_POSSESSIVE}\s+{_PERSON_TRAIT_NOUN_PHRASE}\b",
    re.IGNORECASE,
)
_PERSONAL_ROLE_TRAIT_RE = re.compile(
    # Keep the connector closed so an intervening technical object owns its
    # adjective/noun ("developer fixed the test's unreliability"), not the person.
    rf"\b{_PERSON_ACTOR_NOUN}\b\s+"
    rf"(?:(?:shows?|showed|shown|displays?|displayed|demonstrates?|demonstrated|"
    rf"exhibits?|exhibited|has|have|had)\s+)?{_PERSON_TRAIT_NOUN_PHRASE}\b",
    re.IGNORECASE,
)
_PERSONAL_TRAIT_BY_ROLE_RE = re.compile(
    rf"\b{_PERSON_TRAIT_NOUN}\s+by\s+(?:the\s+)?{_PERSON_ACTOR_NOUN}\b",
    re.IGNORECASE,
)
_PERSONAL_PRONOUN_POSSESSIVE_RE = re.compile(
    rf"\b(?:their|his|her)\s+{_PERSON_TRAIT_NOUN_PHRASE}\b",
    re.IGNORECASE,
)
_PERSONAL_PROCRASTINATION_RE = re.compile(
    rf"\b{_PERSON_ACTOR}\b(?:\W+\w+){{0,4}}\W+procrastinat(?:e|es|ed|ing)\b",
    re.IGNORECASE,
)
_PERSONAL_PRENOMINAL_RE = re.compile(
    rf"\b{_PERSON_TRAIT_ADJECTIVE}\s+{_PERSON_ACTOR_NOUN}\b",
    re.IGNORECASE,
)
_PERSONAL_DIRECTIVE_RE = re.compile(
    rf"\b(?:(?:stop|avoid)\s+(?:being|acting|behaving)\s+|"
    rf"(?:do(?:n't| not)\s+)?be\s+(?:less\s+)?){_PERSON_TRAIT}\b",
    re.IGNORECASE,
)
_BARE_PERSONAL_TITLE_RE = re.compile(
    rf"^\s*(?:(?:chronic|repeated|poor|weak)\s+)?{_PERSON_TRAIT}\s*$",
    re.IGNORECASE,
)


def has_unsupported_personal_claim(rule: SkillRule) -> bool:
    """Return whether rule wording evaluates a person instead of agent behavior."""
    fields = (rule.display_title(), rule.trigger, rule.guidance, rule.why)
    all_text = "\n".join(fields)
    if _UNSUPPORTED_PERSONAL_CLAIM_RE.search(all_text):
        return True

    # ``why`` is declarative and is printed as an observed cost. Second-person
    # wording turns a trace observation into a claim about the reader.
    if _SECOND_PERSON_WHY_RE.search(rule.why):
        return True

    personal_patterns = (
        _PERSONAL_COPULA_RE,
        _PERSONAL_POSSESSIVE_RE,
        _PERSONAL_ROLE_TRAIT_RE,
        _PERSONAL_TRAIT_BY_ROLE_RE,
        _PERSONAL_PRONOUN_POSSESSIVE_RE,
        _PERSONAL_PROCRASTINATION_RE,
        _PERSONAL_PRENOMINAL_RE,
        _PERSONAL_DIRECTIVE_RE,
    )
    if any(pattern.search(field) for pattern in personal_patterns for field in fields):
        return True
    return bool(_BARE_PERSONAL_TITLE_RE.search(rule.display_title()))


def find_rule_policy_violations(rule: SkillRule) -> list[str]:
    """Return stable reasons a rule must not be previewed, persisted, or installed."""
    return [UNSUPPORTED_PERSONAL_CLAIM] if has_unsupported_personal_claim(rule) else []
