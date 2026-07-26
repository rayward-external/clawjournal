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
    r"(?:users?|developers?|engineers?|employees?|persons?|people|humans?|"
    r"programmers?|workers?|coders?|authors?|reviewers?|maintainers?|"
    r"contributors?|teammates?|colleagues?|co-?workers?|collaborators?)"
)
# Nouns that only ever denote a person in SUBJECT position but double as technical
# modifiers elsewhere ("dev server", "dev branch").  They join the clause-level actor
# alternation, which requires a following copula/verb, but must stay out of
# ``_PERSON_ACTOR_NOUN`` — that one also feeds the attributive check, where adding
# them would reject ordinary phrases like "unreliable dev server".
_HUMAN_ROLE_SUBJECT_ONLY = r"(?:devs?|folks)"
# "whoever wrote this", "whoever reviewed the diff" — an indefinite human subject the
# role list cannot enumerate. Bounded so it cannot swallow a whole sentence.
_INDEFINITE_HUMAN_SUBJECT = r"(?:whoever(?:\s+\w+){1,3})"
# "I"/"me" are matched case-SENSITIVELY: a case-insensitive \bi\b matches the loop
# variable that appears all over ordinary coding lessons ("reset i before the loop").
_FIRST_PERSON_SUBJECT = r"(?:(?-i:I)|(?-i:me))"
_PERSON_ACTOR = (
    rf"(?:you|we|they|he|she|{_FIRST_PERSON_SUBJECT}|{_INDEFINITE_HUMAN_SUBJECT}|"
    rf"{_HUMAN_ROLE}|{_HUMAN_ROLE_SUBJECT_ONLY}|(?:coding\s+)?agents?)"
)
_PERSON_ACTOR_NOUN = rf"(?:{_HUMAN_ROLE}|(?:coding\s+)?agents?)"
_PLURAL_HUMAN_ANTECEDENT_RE = re.compile(
    r"\b(?:we|people|persons|users|developers|engineers|employees|programmers|"
    r"workers|coders|authors|reviewers|maintainers|contributors|agents|"
    r"humans|teammates|colleagues|co-?workers|collaborators|devs|folks|"
    r"(?:he|she)\s+(?:and|or)\s+(?:he|she)|"
    r"(?:user|developer|engineer|employee|programmer|worker|coder|author|"
    r"reviewer|maintainer|contributor|agent|human|teammate|colleague|"
    r"co-?worker|collaborator|dev)\s+and\s+"
    r"(?:a\s+|an\s+|the\s+)?(?:user|developer|engineer|employee|programmer|"
    r"worker|coder|author|reviewer|maintainer|contributor|agent|human|"
    r"teammate|colleague|co-?worker|collaborator|dev))\b",
    re.IGNORECASE,
)
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
    rf"\b{_PERSON_ACTOR}\b\s+(?:(?:is|am|are|was|were|seems?|appears?|became|"
    rf"remain(?:s|ed)?|has\s+been|have\s+been|had\s+been)\s+"
    rf"{_PERSON_MODIFIER}(?:{_PERSON_TRAIT_ADJECTIVE}|{_PERSON_TRAIT_NOUN})|"
    rf"(?:acts?|acted|behaves?|behaved)\s+"
    rf"{_PERSON_MODIFIER}{_PERSON_TRAIT_ADVERB})\b",
    re.IGNORECASE,
)
_PERSON_POSSESSIVE = (
    rf"(?:your|their|his|her|my|our|{_PERSON_ACTOR_NOUN}(?:['’]s|['’]))"
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
    rf"\b(?:their|his|her|my|our)\s+{_PERSON_TRAIT_NOUN_PHRASE}\b",
    re.IGNORECASE,
)
_PERSONAL_PROCRASTINATION_RE = re.compile(
    rf"\b{_PERSON_ACTOR}\b(?:\W+\w+){{0,4}}\W+procrastinat(?:e|es|ed|ing)\b",
    re.IGNORECASE,
)
# _PERSONAL_PRENOMINAL_RE is defined after _TECHNICAL_OBJECT_RE, which it needs to
# tell a head noun from an attributive modifier ("careless user" vs "user tests").
_AGENT_ACTOR_RE = re.compile(r"(?:the\s+)?(?:coding\s+)?agents?", re.IGNORECASE)
_PERSONAL_PATTERN_OBJECT_RE = re.compile(
    r"^(?:an?|the)?\s*(?:long|clear|consistent|repeated|chronic)?\s*"
    r"(?:history|habit|pattern|tendency|track\s+record|propensity)\s+(?:of|to|for)\b",
    re.IGNORECASE,
)
_SELF_OR_AGENT_ACTOR_RE = re.compile(
    r"(?:you|we|(?-i:I)|(?:the\s+)?(?:coding\s+)?agents?)", re.IGNORECASE
)
_ANY_PERSONAL_TRAIT_RE = re.compile(
    rf"\b(?:{_PERSON_TRAIT}|arrogan(?:t|ce)|dishonest|foolish|immature|incapable|"
    r"shortsighted|unethical|unmotivated|unprofessional|incompeten(?:t|ce)|"
    r"laziness|hubris)\b",
    re.IGNORECASE,
)
_PREDICATE_CONTINUATION_RE = re.compile(
    r"^(?:and|or|but|nor|yet|with|without|which|who|whose|plus|"
    r"as\s+well\s+as|along\s+with)\b",
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
    rf"\b(?:{_PERSON_TRAIT_ADVERB}|arrogantly|badly|blindly|dishonestly|"
    r"foolishly|hastily|hurriedly|immaturely|poorly|stupidly|thoughtlessly|"
    r"unethically|unprofessionally)\b",
    re.IGNORECASE,
)
_PERSONAL_DECISION_RE = re.compile(
    r"\b(?:poor|bad|weak|questionable|careless|flawed|foolish|reckless|"
    r"shortsighted|unsound)\s+"
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
    r"\b(?:after|although|as|because|before|despite|if|so\s+that|that|when|"
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
    rf"(?=\b(?P<actor>{_PERSON_ACTOR})\b\s+"
    r"(?:is|am|are|was|were|seems?|appears?|became|remain(?:s|ed)?|"
    r"has\s+been|have\s+been|had\s+been|"
    r"(?:can|could|may|might|will|would|should|must)\s+be)\s+"
    r"(?P<predicate>[^.;\n]+))",
    re.IGNORECASE,
)
_PERSON_LINK_AFTER_MODIFIER_RE = re.compile(
    rf"(?=\b(?P<actor>{_PERSON_ACTOR})\b\s*"
    r"(?:(?:[\[({,]|--?|[—–])\s*){0,2}"
    r"(?:after|although|as|because|before|despite|if|that|when|which|while|"
    r"who|whom|whose|where)\b"
    r"(?P<modifier>[^.;]{0,512})"
    r"\b(?P<link>is|am|are|was|were|seems?|appears?|became|remain(?:s|ed)?|"
    r"has\s+been|have\s+been|had\s+been|"
    r"(?:can|could|may|might|will|would|should|must)\s+be)\s+"
    r"(?P<predicate>[^.;\n]+))",
    re.IGNORECASE,
)
_PERSON_ATTRIBUTION_RE = re.compile(
    rf"(?=\b(?P<actor>{_PERSON_ACTOR})\b\s+"
    r"(?P<verb>has|have|had|lacks?|lacked|shows?|showed|shown|"
    r"displays?|displayed|demonstrates?|demonstrated|exhibits?|exhibited|"
    r"exercises?|exercised|possesses?|possessed)\s+"
    r"(?P<object>[^.;\n]+))",
    re.IGNORECASE,
)
_PERSON_POSSESSIVE_LINK_RE = re.compile(
    rf"(?=\b{_PERSON_POSSESSIVE}\s+(?P<subject>[^.;,\n]+?)\s+"
    r"(?:is|am|are|was|were|seems?|appears?|became|remained)\s+[^.;,\n]+)",
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
    r"^(?:(?:not|never)\s+)?(?:awaiting|missing|requesting|seeking)\b",
    re.IGNORECASE,
)
# Transient states of the WORK that take no object. "when you are unsure, ask
# first" is the canonical shape of a good rule, but the object-taking branch
# above demands a technical noun beside the state, and "unsure" never has one —
# so the identical lesson passed or failed on word choice ("uncertain about the
# schema" was safe, "unsure" was an evaluation). A trait complement cannot reach
# here: _UNSAFE_LINK_COMPLEMENT_RE is checked first.
_SAFE_STANDALONE_STATE_RE = re.compile(
    r"^(?:(?:not|never)\s+)?(?:blocked|done|finished|ready|stuck|unclear|"
    r"uncertain|unsure|waiting)\b",
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
    r"\b(?:access|accounts?|apis?|approvals?|artifacts?|answers?|arguments?|args?|"
    r"assertions?|assumptions?|auth(?:entication|orizations?)?|"
    r"authenticate|branch(?:es)?|buffers?|builds?|caches?|callbacks?|callers?|"
    r"certificates?|certs?|changes?|checkouts?|ci|classes|code|columns?|commands?|"
    r"comments?|commits?|configs?|configuration|confirmations?|connections?|connectivity|"
    r"consent|constants?|containers?|"
    r"context|coverage|credentials?|data|databases?|dependenc(?:y|ies)|deployments?|"
    r"diffs?|directories|directory|docs?|documentation|endpoints?|entr(?:y|ies)|"
    r"environments?|errors?|evidence|exceptions?|feedback|fields?|files?|flags?|"
    r"findings?|fix(?:es)?|fixtures?|folders?|functions?|goals?|graphs?|handlers?|"
    r"headers?|hooks?|implementations?|imports?|indexes|indices|information|inputs?|"
    r"interfaces?|instructions?|investigations?|issues?|jobs?|keys?|"
    r"locks?|logs?|merges?|messages?|methods?|metrics?|modules?|"
    r"migrations?|models?|outputs?|packages?|parameters?|params?|patches?|paths?|"
    r"permissions?|"
    r"pipelines?|ports?|preferences?|projects?|prompts?|quer(?:y|ies)|questions?|queues?|"
    r"rebases?|records?|releases?|repos?|repositor(?:y|ies)|"
    r"reports?|requests?|responses?|results?|reviews?|routes?|rows?|runs?|schemas?|"
    r"scripts?|secrets?|"
    r"services?|sessions?|snapshots?|sockets?|states?|status(?:es)?|streams?|suites?|"
    r"symbols?|tables?|tags?|tasks?|tests?|threads?|time|tokens?|tools?|"
    r"traces?|types?|uis?|urls?|validation|values?|variables?|verification|versions?|"
    r"worktrees?|workflows?)\b",
    re.IGNORECASE,
)
_PERSONAL_PRENOMINAL_RE = re.compile(
    # "careless developer" evaluates a person; "unreliable user tests" does not —
    # there ``user`` modifies a technical head noun and the trait describes the
    # tests. Without a head check the gate read the second as "unreliable user"
    # and silently dropped an ordinary lesson.
    #
    # The exemption is deliberately limited to ``user``/``agent``: those are the
    # role nouns that routinely modify a technical head in this domain ("user
    # input", "user config", "agent responses"). Allowing it for every role would
    # reopen the judgment as long as some technical noun followed — "careless
    # developer commits" would pass. A trailing possessive ("the careless user's
    # patch") also still evaluates the person, so it stays denied.
    # The trait must also be one that can sensibly describe an artifact. Of this
    # list only "unreliable" does; a config cannot be negligent or lazy, so
    # "negligent user configs" is still an evaluation of whoever wrote them.
    # The head noun is a closed list of words that are unambiguously nouns here.
    # A general "any technical word" test also matches verbs — "an unreliable
    # user commits secrets" and "unreliable users review nothing" would read as
    # noun phrases and the judgment would pass.
    rf"\b(?!unreliable\s+(?:user|(?:coding\s+)?agent)\s+"
    rf"(?:tests?|input|inputs|output|outputs|responses?|data|config|configs|"
    rf"configuration|sessions?|messages?|prompts?|feedback|settings?)\b)"
    rf"{_PERSON_TRAIT_ADJECTIVE}\s+{_PERSON_ACTOR_NOUN}\b",
    re.IGNORECASE,
)
_TECHNICAL_MODIFIER = (
    r"(?:backend|failing|flaky|fresh|frontend|integration|local|production|"
    r"remote|regression|security|slow|stale|unit)"
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
_SAFE_ACTION_MANNER_PREFIX_RE = re.compile(
    rf"^(?:[a-z][a-z-]*ly\s+){{1,2}}(?={_SAFE_ACTION_GERUND}\b)",
    re.IGNORECASE,
)
_TECHNICAL_ADVERBIAL_PARTICIPLE_RE = re.compile(
    rf"[a-z][a-z-]*ly\s+(?:configured|encoded|escaped|formatted|generated|"
    rf"ordered|performing|quoted|rendered|serialized|sorted|structured|typed|"
    rf"written)\s+[^.;,\n]{{0,40}}{_TECHNICAL_OBJECT_RE.pattern}",
    re.IGNORECASE,
)
_TECHNICAL_OBJECT_MODIFIER_GOVERNOR_RE = re.compile(
    r"\b(?:analyz(?:e|es|ed|ing)|check(?:s|ed|ing)?|compar(?:e|es|ed|ing)|"
    r"fix(?:es|ed|ing)?|inspect(?:s|ed|ing)?|pars(?:e|es|ed|ing)|"
    r"read(?:s|ing)?|review(?:s|ed|ing)?|scan(?:s|ned|ning)?|"
    r"validat(?:e|es|ed|ing))\b",
    re.IGNORECASE,
)
_EMBEDDED_TECHNICAL_SUBJECT_RE = re.compile(
    rf"\b(?P<verb>[a-z][a-z-]*)\s+(?:that\s+)?"
    rf"(?P<subject>(?:(?:their|this|these|those|his|her)\s+)?"
    rf"{_TECHNICAL_NOUN_PHRASE}(?:\s+itself)?)\s*[,)\]}}—–-]*\s*$",
    re.IGNORECASE,
)
_EARLIER_MODIFIER_ACTION_RE = re.compile(
    rf"\b(?:{_SAFE_ACTION_GERUND}|[a-z][a-z-]*ed|did|found|made|ran|read|"
    r"said|saw|told|wrote)\b",
    re.IGNORECASE,
)
_MODIFIER_PERSONAL_STATE_RE = re.compile(
    rf"\b(?:called|considered|deemed|despite\s+being|labeled|"
    rf"regarded(?:\s+as)?|while)\s+"
    rf"{_PERSON_MODIFIER}(?:{_PERSON_TRAIT}|{_PERSONAL_DESCRIPTOR_RE.pattern}|"
    rf"inept|mediocre)\b",
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


def _is_technical_object_modifier(tail: str, start: int) -> bool:
    phrase = _TECHNICAL_ADVERBIAL_PARTICIPLE_RE.match(tail, start)
    if not phrase:
        return False
    prefix = tail[:start]
    governors = list(_TECHNICAL_OBJECT_MODIFIER_GOVERNOR_RE.finditer(prefix))
    if not governors:
        return False
    suffix = prefix[governors[-1].end():]
    return re.search(r"\b(?:and|but|or|then|yet)\b", suffix, re.IGNORECASE) is None


def _has_personal_action_adverb(tail: str) -> bool:
    for match in _PERSONAL_ACTION_ADVERB_RE.finditer(tail):
        if _is_technical_object_modifier(tail, match.start()):
            continue
        return True
    return False


def _has_technical_pronoun_antecedent(text: str, offset: int) -> bool:
    """Return whether ``they`` follows a technical, not human, clause subject."""
    clauses = re.split(r"[.;\n]", text[:offset])
    clause = next((part for part in reversed(clauses) if part.strip()), "")
    technical_refs = list(_TECHNICAL_OBJECT_RE.finditer(clause))
    if not technical_refs:
        return False
    human_refs = re.findall(
        rf"\b(?:he|she|him|her|{_PERSON_ACTOR_NOUN})\b",
        clause,
        re.IGNORECASE,
    )
    if _PLURAL_HUMAN_ANTECEDENT_RE.search(clause) or len(human_refs) >= 2:
        return False
    singular_s_words = {"access", "analysis", "business", "class", "process", "status"}
    has_plural_technical = any(
        (word := re.findall(r"[a-z]+", match.group().casefold()))
        and word[-1].endswith("s")
        and word[-1] not in singular_s_words
        for match in technical_refs
    )
    if has_plural_technical:
        return True
    return not re.search(rf"\b{_PERSON_ACTOR}\b", clause, re.IGNORECASE)


def _has_personal_clause(text: str) -> bool:
    for match in _HUMAN_CLAUSE_RE.finditer(text):
        tail = match.group("tail")
        boundary = _SUBORDINATE_BOUNDARY_RE.search(tail)
        if boundary:
            tail = tail[:boundary.start()]
        if (
            _has_personal_action_adverb(tail)
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
    actor: str = "",
) -> bool:
    normalized = re.sub(r"\s+", " ", component.replace("’", "'")).strip()
    normalized = _SAFE_PREFIX_RE.sub("", normalized)
    manner_prefix = _SAFE_ACTION_MANNER_PREFIX_RE.match(normalized)
    if manner_prefix and _PERSONAL_ACTION_ADVERB_RE.search(manner_prefix.group()):
        return False
    if manner_prefix:
        normalized = normalized[manner_prefix.end():]
    elif _PERSONAL_ACTION_ADVERB_RE.match(normalized):
        return False
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
    # A bare state is safe only for the addressee or the agent: "when you are
    # unsure" reports the state of the work in hand. "the reviewer is unsure
    # about nearly every change" is an evaluation of a third party, so a
    # third-person human subject keeps needing a technical object.
    if _SAFE_STANDALONE_STATE_RE.match(normalized) and _SELF_OR_AGENT_ACTOR_RE.fullmatch(
        actor.strip()
    ):
        return True
    if _SAFE_RESOURCE_STATE_RE.search(normalized) or _SAFE_PREPOSITION_RE.search(normalized):
        return _TECHNICAL_OBJECT_RE.search(normalized) is not None
    return bool(
        allow_inherited_object
        and _BARE_TECHNICAL_COMPONENT_RE.fullmatch(normalized)
    )


def _predicate_within_clause(text: str, actor_start: int, predicate: str) -> str:
    """Trim a captured predicate to the clause the linking verb actually governs.

    The linking patterns capture to the end of the sentence, so in "when you are
    unsure, ask before proceeding" the predicate came out as "unsure, ask before
    proceeding" — the main instruction after the comma was judged as part of the
    complement, and ordinary rules were rejected for it. When the actor sits in a
    subordinate clause, that clause closes at the first comma.

    This cannot hide a judgment that really does follow the comma: the linking
    patterns scan with a lookahead, so a later "<actor> is <trait>" clause is
    matched independently.
    """
    head = re.split(r"[.;\n]", text[:actor_start])[-1]
    if not _SUBORDINATE_BOUNDARY_RE.search(head):
        return predicate
    bounded, sep, rest = predicate.partition(",")
    if not sep:
        return predicate
    # Only a genuinely new clause ends the complement. "…, and has a long history
    # of approving blindly" or "…, with a poor record" continues the SAME
    # predicate, so truncating there would hide the judgment that follows.
    if _PREDICATE_CONTINUATION_RE.match(rest.strip()):
        return predicate
    # Truncating is only ever a convenience for a benign main clause. If anything
    # after the comma carries trait vocabulary ("…, that is usually overconfidence"),
    # keep the whole predicate so it is still judged.
    if _ANY_PERSONAL_TRAIT_RE.search(rest):
        return predicate
    return bounded


def _is_safe_link_predicate(predicate: str, actor: str = "") -> bool:
    parts = _components(predicate)
    if not parts or not _is_safe_link_component(parts[0], actor=actor):
        return False
    # In "fixing code and tests", the later bare technical object inherits the
    # first component's recognized action. It is never sufficient on its own.
    return all(
        _is_safe_link_component(part, allow_inherited_object=True, actor=actor)
        for part in parts[1:]
    )


def _is_safe_attribution_object(obj: str, actor: str = "") -> bool:
    # "has a history/habit/pattern of <anything>" claims a standing disposition,
    # so it stays a personal claim however technical the rest of the phrase is —
    # the component test below only asks whether a technical noun appears
    # somewhere, which a widened noun list makes easy to satisfy.
    # A coding agent is exempt: describing what the agent repeatedly does is the
    # whole point of an "avoid" rule.
    if _PERSONAL_PATTERN_OBJECT_RE.match(obj.strip()):
        return bool(
            _AGENT_ACTOR_RE.fullmatch(actor.strip())
            and not _ANY_PERSONAL_TRAIT_RE.search(obj)
        )
    parts = _components(obj)
    return bool(parts) and all(
        _TECHNICAL_OBJECT_RE.search(_head_chunk(part))
        or _SAFE_ATTRIBUTION_ACTION_RE.search(part)
        for part in parts
    )


def _head_chunk(part: str) -> str:
    """The head of a noun phrase: everything before its first preposition.

    "cache" and "test suite" are technical subjects; "memory of the folder
    layout", "grasp of the module" and "refusal to read the comments" are not —
    their heads are personal nouns and only the trailing prepositional phrase is
    technical. Searching the whole phrase for any technical word lets that
    trailing phrase launder the claim, so match on the head alone.
    """
    return re.split(
        r"\b(?:of|about|to|for|with|regarding|concerning|around|over)\b",
        part, maxsplit=1,
    )[0]


def _possessed_subject_is_technical(subject: str) -> bool:
    parts = _components(subject)
    return bool(parts) and all(
        _TECHNICAL_OBJECT_RE.search(_head_chunk(part)) for part in parts
    )


def _modifier_has_human_evaluation(actor: str, modifier: str) -> bool:
    cleaned = modifier.strip(" \t,()[]{}—–-")
    if not cleaned:
        return False
    if _MODIFIER_PERSONAL_STATE_RE.search(cleaned):
        return True
    synthetic = f"{actor} {cleaned}"
    if _has_personal_clause(synthetic):
        return True
    for match in _PERSON_LINK_RE.finditer(synthetic):
        if not _is_safe_link_predicate(match.group("predicate")):
            return True
    for match in _PERSON_ATTRIBUTION_RE.finditer(synthetic):
        if not _is_safe_attribution_object(match.group("object").strip()):
            return True
    return False


def _subject_agrees_with_link(subject: str, link: str) -> bool:
    words = re.findall(r"[a-z]+", subject.casefold())
    if not words:
        return False
    while words and words[-1] == "itself":
        words.pop()
    if not words:
        return False
    singular_s_words = {"access", "analysis", "business", "class", "process", "status"}
    subject_is_plural = (
        words[-1] == "data"
        or (words[-1].endswith("s") and words[-1] not in singular_s_words)
    )
    normalized_link = re.sub(r"\s+", " ", link.casefold()).strip()
    singular_link = normalized_link in {
        "is", "was", "seems", "appears", "became", "remains", "remained", "has been",
    }
    plural_link = normalized_link in {
        "are", "were", "seem", "appear", "remain", "have been",
    }
    if singular_link:
        return not subject_is_plural
    if plural_link:
        return subject_is_plural
    return True


def _actor_agrees_with_link(actor: str, link: str) -> bool:
    normalized_actor = re.sub(r"\s+", " ", actor.casefold()).strip()
    if normalized_actor in {"you"}:
        return True
    actor_is_plural = (
        normalized_actor in {"we", "they", "people", "persons"}
        or normalized_actor.endswith("s")
    )
    normalized_link = re.sub(r"\s+", " ", link.casefold()).strip()
    if normalized_link in {
        "is", "was", "seems", "appears", "became", "remains", "remained", "has been",
    }:
        return not actor_is_plural
    if normalized_link in {"are", "were", "seem", "appear", "remain", "have been"}:
        return actor_is_plural
    return True


def _modifier_ends_with_embedded_technical_subject(
    actor: str,
    modifier: str,
    link: str,
) -> bool:
    if modifier.rstrip().endswith((",", ")", "]", "}", "—", "–", "-")):
        return False
    cleaned = modifier.strip(" \t,()[]{}—–-")
    match = _EMBEDDED_TECHNICAL_SUBJECT_RE.search(cleaned)
    if not match or not _subject_agrees_with_link(match.group("subject"), link):
        return False
    earlier = cleaned[:match.start()]
    if re.search(r"\b(?:and|but|or|then|yet)\b", earlier, re.IGNORECASE):
        return False
    return bool(
        _EARLIER_MODIFIER_ACTION_RE.search(earlier)
        or _TECHNICAL_OBJECT_MODIFIER_GOVERNOR_RE.search(earlier)
        or not _actor_agrees_with_link(actor, link)
    )


def _has_structural_human_evaluation(text: str) -> bool:
    if _has_personal_clause(text):
        return True
    for match in _PERSON_LINK_RE.finditer(text):
        if (
            match.group("actor").casefold() == "they"
            and _has_technical_pronoun_antecedent(text, match.start())
        ):
            continue
        if not _is_safe_link_predicate(
            _predicate_within_clause(text, match.start(), match.group("predicate")),
            match.group("actor"),
        ):
            return True
    for match in _PERSON_LINK_AFTER_MODIFIER_RE.finditer(text):
        if _modifier_has_human_evaluation(
            match.group("actor"),
            match.group("modifier"),
        ):
            return True
        if (
            match.group("actor").casefold() == "they"
            and _has_technical_pronoun_antecedent(text, match.start())
        ):
            continue
        if _modifier_ends_with_embedded_technical_subject(
            match.group("actor"),
            match.group("modifier"),
            match.group("link"),
        ):
            continue
        if not _is_safe_link_predicate(
            _predicate_within_clause(text, match.start(), match.group("predicate")),
            match.group("actor"),
        ):
            return True
    for match in _PERSON_ATTRIBUTION_RE.finditer(text):
        obj = match.group("object").strip()
        # ``has been`` is handled by the linking-clause pass above.
        if match.group("verb").lower() in {"has", "have", "had"} and re.match(
            r"^been\b", obj, re.IGNORECASE
        ):
            continue
        if not _is_safe_attribution_object(obj, match.group("actor")):
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
