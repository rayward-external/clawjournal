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
    r"programmers?|workers?|coders?|authors?|reviewers?|maintainers?|"
    r"contributors?)"
)
_PERSON_ACTOR = rf"(?:you|we|they|he|she|{_HUMAN_ROLE}|(?:coding\s+)?agents?)"
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
    rf"(?:your|their|his|her|{_PERSON_ACTOR_NOUN}(?:['’]s|['’]))"
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

# Personal-only concepts are checked when a human actor governs them. This
# catches varied evaluation verbs while leaving qualities owned by a technical
# artifact or an external decision available as technical context.
_PERSONAL_QUALITY_RE = re.compile(
    r"\b(?:arrogance|attitude|aptitude|competenc(?:e|y)|conscientiousness|"
    r"diligence|discipline|honesty|hubris|judg(?:e)?ment|motivation|"
    r"professionalism|shortsightedness|temperament|wisdom|work\s+ethic)\b",
    re.IGNORECASE,
)
_PERSONAL_DESCRIPTOR_RE = re.compile(
    r"\b(?:arrogant|dishonest|diligent|foolish|immature|incapable|reckless|"
    r"shortsighted|unethical|unmotivated|unprofessional(?:ism)?(?!-looking))\b",
    re.IGNORECASE,
)
_PERSONAL_ACTION_ADVERB_RE = re.compile(
    r"\b(?:arrogantly|carelessly|dishonestly|foolishly|immaturely|"
    r"incompetently|negligently|recklessly|sloppily|unethically|"
    r"unprofessionally)\b",
    re.IGNORECASE,
)
_PERSONAL_DECISION_RE = re.compile(
    r"\b(?:poor|bad|weak|questionable|flawed|reckless|shortsighted)\s+"
    r"(?:decisions?|choices?|decision-making)\b",
    re.IGNORECASE,
)
_PERSONAL_OWNERSHIP_RE = re.compile(
    r"\b(?:poor|bad|weak|questionable|insufficient|no|little)\s+ownership\b",
    re.IGNORECASE,
)
_PERSONAL_PERFORMANCE_RE = re.compile(
    rf"\b{_PERSON_ACTOR}\b\s+(?:(?:performs?|performed|works?|worked)|"
    r"(?:is|are|was|were)\s+(?:performing|working))\s+(?:poorly|badly)\b",
    re.IGNORECASE,
)
_PERSONAL_JOB_RE = re.compile(
    rf"\b{_PERSON_ACTOR}\b\s+(?:did|does|do)\s+(?:a\s+)?"
    r"(?:poor|bad|terrible|awful)\s+job\b",
    re.IGNORECASE,
)
_PERSONAL_WORK_RE = re.compile(
    rf"\b{_PERSON_ACTOR}\b\s+(?:delivers?|delivered|produces?|produced|"
    r"submits?|submitted)\s+(?:poor|bad|terrible|awful)\s+work\b",
    re.IGNORECASE,
)
_PERSONAL_GOVERNOR_RE = re.compile(
    r"(?:^['’]s\b|\b(?:is|are|was|were|be|been|being|has|have|had|"
    r"lack|lacks|lacked|show|shows|showed|display|displays|displayed|"
    r"demonstrate|demonstrates|demonstrated|exhibit|exhibits|exhibited|"
    r"exercise|exercises|exercised|need|needs|needed|with|of|"
    r"assess|assesses|assessed|evaluate|evaluates|evaluated|"
    r"judge|judges|judged|question|questions|questioned)\b)"
    r"[^.;,\n]{0,60}$",
    re.IGNORECASE,
)
_DECISION_GOVERNOR_RE = re.compile(
    r"(?:^['’]s\b|\b(?:has|have|had|makes?|made|making|with)\b)"
    r"[^.;,\n]{0,50}$",
    re.IGNORECASE,
)
_TECHNICAL_QUALITY_OWNER_RE = re.compile(
    r"\b(?:agent|artifact|component|model|report|service|system|test)\s+$",
    re.IGNORECASE,
)
_EXTERNAL_DECISION_OWNER_RE = re.compile(
    r"^\s+(?:made|produced|returned)\s+by\b",
    re.IGNORECASE,
)
_HUMAN_CLAUSE_RE = re.compile(
    rf"\b{_PERSON_ACTOR}\b(?P<tail>[^.;,\n]{{0,180}})",
    re.IGNORECASE,
)
_SUBORDINATE_BOUNDARY_RE = re.compile(
    r"\b(?:after|although|as|because|before|if|so\s+that|that|when|"
    r"which|while|who|where)\b",
    re.IGNORECASE,
)
_PERSONAL_DESCRIPTOR_ROLE_RE = re.compile(
    rf"\b{_PERSONAL_DESCRIPTOR_RE.pattern}\s+{_PERSON_ACTOR_NOUN}\b",
    re.IGNORECASE,
)
_PERSONAL_ROLE_DESCRIPTOR_RE = re.compile(
    rf"\b{_PERSON_ACTOR_NOUN}\b\s+(?:is\s+)?{_PERSONAL_DESCRIPTOR_RE.pattern}\b",
    re.IGNORECASE,
)
_PERSONAL_EVALUATION_BY_ROLE_RE = re.compile(
    r"\b(?:(?:poor|bad|weak|questionable|flawed|reckless|shortsighted|"
    r"insufficient)\s+(?:judg(?:e)?ment|decisions?|choices?|decision-making|"
    r"ownership|work|job)|(?:lack|absence)\s+of\s+(?:diligence|discipline|"
    r"honesty|professionalism|work\s+ethic))\s+by\s+(?:the\s+)?"
    rf"{_PERSON_ACTOR_NOUN}\b",
    re.IGNORECASE,
)

# Structural fail-closed checks cover arbitrary complements without attempting
# to enumerate every possible English adjective. Concrete coding actions,
# operational states, and technical-resource clauses are the explicit safe set.
_PERSON_LINK_RE = re.compile(
    rf"(?=\b{_PERSON_ACTOR}\b\s+"
    r"(?:is|are|was|were|seems?|appears?|became|remained|"
    r"has\s+been|have\s+been|had\s+been|"
    r"(?:can|could|may|might|will|would|should|must)\s+be)\s+"
    r"(?P<predicate>[^.;\n]+))",
    re.IGNORECASE,
)
_PERSON_ATTRIBUTION_RE = re.compile(
    rf"(?=\b{_PERSON_ACTOR}\b\s+"
    r"(?P<verb>has|have|had|lacks?|lacked|shows?|showed|shown|"
    r"displays?|displayed|demonstrates?|demonstrated|exhibits?|exhibited|"
    r"exercises?|exercised|possesses?|possessed)\s+"
    r"(?P<object>[^.;\n]+))",
    re.IGNORECASE,
)
_PERSON_POSSESSIVE_LINK_RE = re.compile(
    rf"(?=\b{_PERSON_POSSESSIVE}\s+(?P<subject>[^.;,\n]+?)\s+"
    r"(?:is|are|was|were|seems?|appears?|became|remained)\s+[^.;,\n]+)",
    re.IGNORECASE,
)
_PERSON_POSSESSIVE_CONSEQUENCE_RE = re.compile(
    rf"(?=\b{_PERSON_POSSESSIVE}\s+(?P<subject>[^.;,\n]+?)\s+"
    r"(?:causes?|caused|leads?|led|requires?|required|forces?|forced|"
    r"delays?|delayed|creates?|created|produces?|produced|results?|resulted)\b)",
    re.IGNORECASE,
)
_COORDINATE_RE = re.compile(
    r"\s*(?:,|\b(?:and|but|yet|or|while)\b)\s*",
    re.IGNORECASE,
)
_SAFE_PREFIX_RE = re.compile(
    r"^(?:(?:currently|actively|still|already|now|temporarily|repeatedly|"
    r"then)\s+)+",
    re.IGNORECASE,
)
_SAFE_STATUS_RE = re.compile(
    r"^(?:(?:not|never)\s+)?(?:blocked|waiting|ready|available|unavailable|"
    r"online|offline|authenticated|unauthenticated|authorized|unauthorized|"
    r"assigned|connected|disconnected|active|inactive|idle|busy|stuck|"
    r"allowed|denied|required|expected|scheduled|configured|enabled|disabled|"
    r"finished|done|complete|asked|given|prompted|told|invited|"
    r"(?:signed|logged)\s+(?:in|out))\b",
    re.IGNORECASE,
)
_SAFE_ACTION_GERUND = (
    r"(?:addressing|analyzing|asking|building|changing|"
    r"checking|choosing|clarifying|committing|comparing|considering|correcting|"
    r"creating|debugging|deciding|deleting|deploying|editing|fixing|generating|"
    r"handling|implementing|inspecting|installing|investigating|loading|making|"
    r"merging|modifying|opening|parsing|preparing|pushing|reading|recovering|"
    r"refactoring|rendering|reporting|reproducing|requesting|resolving|"
    r"responding|retrying|reviewing|"
    r"rerunning|running|scanning|sending|testing|trying|updating|using|"
    r"validating|verifying|waiting|working|writing)"
)
_SAFE_ACTION_RE = re.compile(
    rf"^(?:(?:not|never)\s+)?{_SAFE_ACTION_GERUND}\b|"
    r"^(?:(?:not|never)\s+)?failing\s+to\b",
    re.IGNORECASE,
)
_SAFE_OWNERSHIP_RE = re.compile(
    r"^(?:responsible|accountable)\s+(?:for|to)\b|"
    r"^(?:the\s+)?(?:owner|maintainer|assignee|operator|lead)\s+(?:of|for)\b",
    re.IGNORECASE,
)
_SAFE_RESOURCE_STATE_RE = re.compile(
    r"^(?:(?:not|never)\s+)?(?:able|unable)\s+to\b|"
    r"^(?:(?:not|never)\s+)?(?:awaiting|missing|requesting|seeking|"
    r"uncertain)\b",
    re.IGNORECASE,
)
_SAFE_PREPOSITION_RE = re.compile(
    r"^(?:(?:not|never)\s+)?(?:about\s+to|going\s+to|supposed\s+to|"
    r"in|on|at|under|behind|ahead|due)\b",
    re.IGNORECASE,
)
_UNSAFE_LINK_COMPLEMENT_RE = re.compile(
    rf"^(?:(?:not|never)\s+)?(?:(?:a|an|the)\s+)?"
    rf"(?:poor|bad|weak|terrible|awful|{_PERSONAL_DESCRIPTOR_RE.pattern})\b",
    re.IGNORECASE,
)
_TECHNICAL_OBJECT_RE = re.compile(
    r"\b(?:access|accounts?|apis?|approvals?|artifacts?|answers?|"
    r"auth(?:entication|orizations?)?|"
    r"authenticate|branch(?:es)?|builds?|changes?|checkouts?|ci|code|commands?|"
    r"commits?|configs?|configuration|confirmations?|connections?|connectivity|"
    r"consent|containers?|"
    r"context|coverage|credentials?|data|databases?|dependenc(?:y|ies)|deployments?|"
    r"diffs?|docs?|documentation|environments?|errors?|evidence|feedback|files?|"
    r"findings?|fix(?:es)?|fixtures?|goals?|implementations?|information|inputs?|"
    r"interfaces?|instructions?|issues?|jobs?|logs?|merges?|messages?|metrics?|"
    r"migrations?|models?|outputs?|packages?|patches?|paths?|permissions?|"
    r"pipelines?|preferences?|projects?|prompts?|quer(?:y|ies)|questions?|queues?|"
    r"reports?|requests?|responses?|results?|reviews?|runs?|schemas?|scripts?|"
    r"services?|sessions?|states?|status(?:es)?|suites?|tasks?|tests?|time|tools?|"
    r"traces?|uis?|validation|verification|worktrees?|workflows?)\b",
    re.IGNORECASE,
)
_TECHNICAL_MODIFIER = (
    r"(?:backend|failing|flaky|frontend|integration|local|remote|regression|"
    r"security|slow|stale|unit)"
)
_TECHNICAL_NOUN_PHRASE = (
    rf"(?:(?:a|an|another|the|its|our|your)\s+)?"
    rf"(?:{_TECHNICAL_MODIFIER}\s+){{0,2}}"
    rf"{_TECHNICAL_OBJECT_RE.pattern}"
    rf"(?:\s+{_TECHNICAL_OBJECT_RE.pattern})*"
)
_DIRECT_TECHNICAL_OBJECT_PHRASE = (
    rf"(?:(?:a|an|another|the|its|our|your)\s+)?"
    rf"(?:{_TECHNICAL_MODIFIER}\s+){{0,2}}"
    rf"{_TECHNICAL_OBJECT_RE.pattern}"
    rf"(?:\s+{_TECHNICAL_OBJECT_RE.pattern})*"
)
_BARE_TECHNICAL_COMPONENT_RE = re.compile(
    rf"^{_TECHNICAL_NOUN_PHRASE}"
    rf"(?:\s+(?:for|from|in|on|with)\s+{_TECHNICAL_NOUN_PHRASE})?$",
    re.IGNORECASE,
)
_SAFE_TECHNICAL_ROLE_RE = re.compile(
    rf"^(?:(?:a|an|the)\s+)?{_TECHNICAL_OBJECT_RE.pattern}\s+"
    r"(?:maintainer|operator|owner|service)$",
    re.IGNORECASE,
)
_SAFE_MORPHOLOGICAL_ACTION_RE = re.compile(
    r"^(?:(?:not|never)\s+)?"
    r"(?!(?:acting|appearing|becoming|behaving|being|failing|feeling|looking|"
    r"procrastinating|remaining|seeming|slacking|struggling|underperforming)\b)"
    rf"[a-z][a-z-]*ing\s+{_DIRECT_TECHNICAL_OBJECT_PHRASE}$",
    re.IGNORECASE,
)
_SAFE_ATTRIBUTION_ACTION_RE = re.compile(
    rf"^(?:been\s+)?{_SAFE_ACTION_GERUND}\b|"
    r"^to\s+(?:ask|check|choose|clarify|continue|decide|"
    r"pause|proceed|respond|retry|stop|wait)\b",
    re.IGNORECASE,
)


def _components(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text.replace("’", "'")).strip()
    return [part.strip() for part in _COORDINATE_RE.split(normalized) if part.strip()]


def _has_governed_match(tail: str, pattern: re.Pattern[str]) -> bool:
    for match in pattern.finditer(tail):
        prefix = tail[:match.start()]
        if _TECHNICAL_QUALITY_OWNER_RE.search(prefix):
            continue
        if _PERSONAL_GOVERNOR_RE.search(prefix):
            return True
    return False


def _has_governed_decision(tail: str) -> bool:
    for match in _PERSONAL_DECISION_RE.finditer(tail):
        if _EXTERNAL_DECISION_OWNER_RE.match(tail[match.end():]):
            continue
        if _DECISION_GOVERNOR_RE.search(tail[:match.start()]):
            return True
    return False


def _has_personal_clause(text: str) -> bool:
    for match in _HUMAN_CLAUSE_RE.finditer(text):
        tail = match.group("tail")
        boundary = _SUBORDINATE_BOUNDARY_RE.search(tail)
        if boundary:
            tail = tail[:boundary.start()]
        if (
            _PERSONAL_ACTION_ADVERB_RE.search(tail)
            or _has_governed_match(tail, _PERSONAL_QUALITY_RE)
            or _has_governed_match(tail, _PERSONAL_OWNERSHIP_RE)
            or _has_governed_match(tail, _PERSONAL_DESCRIPTOR_RE)
            or _has_governed_decision(tail)
        ):
            return True
    return bool(
        _PERSONAL_DESCRIPTOR_ROLE_RE.search(text)
        or _PERSONAL_EVALUATION_BY_ROLE_RE.search(text)
        or _PERSONAL_PERFORMANCE_RE.search(text)
        or _PERSONAL_JOB_RE.search(text)
        or _PERSONAL_WORK_RE.search(text)
    )


def _is_safe_link_component(
    component: str,
    *,
    allow_inherited_object: bool = False,
) -> bool:
    normalized = re.sub(r"\s+", " ", component.replace("’", "'")).strip()
    normalized = _SAFE_PREFIX_RE.sub("", normalized)
    if _UNSAFE_LINK_COMPLEMENT_RE.search(normalized):
        return False
    if any(pattern.search(normalized) for pattern in (
        _SAFE_STATUS_RE,
        _SAFE_ACTION_RE,
        _SAFE_OWNERSHIP_RE,
    )):
        return True
    if _SAFE_MORPHOLOGICAL_ACTION_RE.fullmatch(normalized):
        return True
    if _SAFE_TECHNICAL_ROLE_RE.fullmatch(normalized):
        return True
    if _SAFE_RESOURCE_STATE_RE.search(normalized) or _SAFE_PREPOSITION_RE.search(normalized):
        return _TECHNICAL_OBJECT_RE.search(normalized) is not None
    return bool(
        allow_inherited_object
        and _BARE_TECHNICAL_COMPONENT_RE.fullmatch(normalized)
    )


def _is_safe_link_predicate(predicate: str) -> bool:
    parts = _components(predicate)
    if not parts or not _is_safe_link_component(parts[0]):
        return False
    # In "fixing code and tests", the later bare technical object inherits the
    # first component's recognized action. It is never sufficient on its own.
    return all(
        _is_safe_link_component(part, allow_inherited_object=True)
        for part in parts[1:]
    )


def _is_safe_attribution_object(obj: str) -> bool:
    parts = _components(obj)
    return bool(parts) and all(
        _TECHNICAL_OBJECT_RE.search(part)
        or _SAFE_ATTRIBUTION_ACTION_RE.search(part)
        for part in parts
    )


def _possessed_subject_is_technical(subject: str) -> bool:
    parts = _components(subject)
    return bool(parts) and all(_TECHNICAL_OBJECT_RE.search(part) for part in parts)


def _has_structural_human_evaluation(text: str) -> bool:
    if _has_personal_clause(text):
        return True
    for match in _PERSON_LINK_RE.finditer(text):
        if not _is_safe_link_predicate(match.group("predicate")):
            return True
    for match in _PERSON_ATTRIBUTION_RE.finditer(text):
        obj = match.group("object").strip()
        # ``has been`` is handled by the linking-clause pass above.
        if match.group("verb").lower() in {"has", "have", "had"} and re.match(
            r"^been\b", obj, re.IGNORECASE
        ):
            continue
        if not _is_safe_attribution_object(obj):
            return True
    for pattern in (_PERSON_POSSESSIVE_LINK_RE, _PERSON_POSSESSIVE_CONSEQUENCE_RE):
        for match in pattern.finditer(text):
            if not _possessed_subject_is_technical(match.group("subject")):
                return True
    return False


def _has_bare_personal_title(title: str) -> bool:
    normalized = re.sub(r"\s+", " ", title).strip()
    return bool(
        _BARE_PERSONAL_TITLE_RE.fullmatch(normalized)
        or _PERSONAL_QUALITY_RE.search(normalized)
        or _PERSONAL_DECISION_RE.search(normalized)
        or _PERSONAL_OWNERSHIP_RE.search(normalized)
        or _PERSONAL_DESCRIPTOR_RE.fullmatch(normalized)
        or _PERSONAL_ROLE_DESCRIPTOR_RE.search(normalized)
    )


def _title_is_guidance_derived(rule: SkillRule) -> bool:
    """Return whether the stored title is schema.py's four-word fallback."""
    if not rule.title.strip():
        return True
    words = re.split(r"\s+", rule.guidance.strip())
    derived = " ".join(words[:4]).rstrip(".,;:—- ")
    return rule.title.strip().casefold() == derived.casefold()


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

    # A fallback title truncates guidance and can omit the technical object that
    # makes an attribution safe. Analyze its full guidance context instead.
    structural_fields = fields[1:] if _title_is_guidance_derived(rule) else fields
    if any(_has_structural_human_evaluation(field) for field in structural_fields):
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
    return _has_bare_personal_title(rule.display_title())


def find_rule_policy_violations(rule: SkillRule) -> list[str]:
    """Return stable reasons a rule must not be previewed, persisted, or installed."""
    return [UNSUPPORTED_PERSONAL_CLAIM] if has_unsupported_personal_claim(rule) else []
