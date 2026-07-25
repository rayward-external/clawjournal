"""Choose one evidence-backed, preview-only coding-agent focus.

The durable skill store keeps ``case-NN`` aliases, but those aliases are scoped
to one distill prompt and can mean something else on a later run.  A focus is
therefore derived only from freshly distilled rules and the current corpus.  It
never adds spotlight framing or evidence metadata to the rendered, installed, or
persisted skill; its underlying rule remains part of the proposed skill set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone

from .distill import _candidate_aliases
from .rule_policy import has_unsupported_personal_claim
from .schema import FAILURE_MODES, SkillRule
from .select import _CLEAN_RECOVERY, SkillCandidate, SkillCorpus, _parse_start_time
from .store import fingerprint

MIN_FOCUS_SESSIONS = 3
MIN_FOCUS_DAYS = 2
MIN_FOCUS_PROJECTS = 2


@dataclass(frozen=True)
class FocusSpotlight:
    """Human-facing evidence summary for one current-run avoid rule."""

    rule: SkillRule
    session_count: int
    day_count: int
    project_count: int


def _matches_direct_failure_evidence(
    rule: SkillRule,
    candidate: SkillCandidate,
) -> bool:
    """Return whether a real candidate structurally witnessed this avoid mode."""
    taxonomy = (rule.taxonomy or "").strip()
    if taxonomy not in FAILURE_MODES or taxonomy not in set(candidate.failure_modes or ()):
        return False
    if candidate.kind == "avoid":
        return True
    if candidate.kind == "do":
        # A normal strong success can have no relationship to an avoid rule. Only the
        # do-pool shape that records a clean recovery is direct failure evidence.
        return bool(_CLEAN_RECOVERY & set(candidate.recovery_labels or ()))
    return False


def _direct_candidates(rule: SkillRule, corpus: SkillCorpus) -> list[SkillCandidate]:
    """Resolve this run's cited aliases to distinct, real session candidates.

    A matching failure-pool candidate counts directly. A do-pool candidate counts
    only when it records both the same failure taxonomy and a clean recovery; this
    retains mistake-to-fix evidence without allowing unrelated strong successes.
    Synthetic candidates and ambiguous aliases remain ineligible.
    """
    aliases = _candidate_aliases(corpus)
    eligible_ids = set(corpus.eligible_session_ids)
    alias_counts: dict[str, int] = {}
    for candidate in corpus.candidates:
        alias = aliases.get(candidate.session_id)
        if alias:
            alias_counts[alias] = alias_counts.get(alias, 0) + 1
    by_alias: dict[str, SkillCandidate] = {}
    for candidate in corpus.candidates:
        alias = aliases.get(candidate.session_id)
        if (
            alias
            and alias_counts.get(alias) == 1
            and candidate.session_id in eligible_ids
            and _matches_direct_failure_evidence(rule, candidate)
        ):
            # Synthetic objective aggregates have placeholder ids which are not in
            # eligible_session_ids.  They may influence a durable rule's support, but
            # one aggregate alias must never masquerade as several direct citations.
            # A duplicate alias is ambiguous (including a real/synthetic id collision),
            # so it is excluded above rather than guessed.
            by_alias[alias] = candidate

    resolved: list[SkillCandidate] = []
    seen_sessions: set[str] = set()
    for case_id in rule.evidence_session_ids:
        candidate = by_alias.get(case_id)
        if candidate is None or candidate.session_id in seen_sessions:
            continue
        seen_sessions.add(candidate.session_id)
        resolved.append(candidate)
    return resolved


def _project_identity(candidate: SkillCandidate) -> str:
    """Normalize source-prefixed display names so one repo counts as one project."""
    project = (candidate.project or "").strip().casefold()
    source_prefix = f"{(candidate.source or '').strip().casefold()}:"
    if source_prefix != ":" and project.startswith(source_prefix):
        project = project[len(source_prefix):]
    return "" if project in {"", "unknown", "~home"} else project


def _spotlight(rule: SkillRule, corpus: SkillCorpus) -> FocusSpotlight | None:
    if rule.kind != "avoid" or not rule.why.strip():
        return None
    if has_unsupported_personal_claim(rule):
        return None

    direct = _direct_candidates(rule, corpus)
    days = set()
    projects = set()
    for candidate in direct:
        parsed = _parse_start_time(candidate.start_time)
        if parsed is not None:
            days.add(parsed.astimezone(timezone.utc).date())
        project = _project_identity(candidate)
        if project:
            projects.add(project)

    if (
        len(direct) < MIN_FOCUS_SESSIONS
        or len(days) < MIN_FOCUS_DAYS
        or len(projects) < MIN_FOCUS_PROJECTS
    ):
        return None
    return FocusSpotlight(
        rule=rule,
        session_count=len(direct),
        day_count=len(days),
        project_count=len(projects),
    )


def select_focus(
    *,
    active_rules: list[SkillRule],
    current_rules: list[SkillRule],
    corpus: SkillCorpus,
) -> FocusSpotlight | None:
    """Return the strongest current-run focus whose exact rule survived every gate.

    Exact fingerprints prevent a fresh rule's ephemeral evidence from attaching to
    a carried paraphrase.  Directly cited session breadth ranks qualifying rules;
    final active-skill order is the deterministic tie-breaker.
    """
    current_by_fp: dict[str, SkillRule] = {}
    for rule in current_rules:
        # merge_rules is last-wins for duplicate exact fingerprints; mirror it so
        # evidence and displayed wording always come from the same fresh proposal.
        current_by_fp[fingerprint(rule)] = rule

    choices: list[tuple[tuple[int, int, int, int], FocusSpotlight]] = []
    for active_index, active_rule in enumerate(active_rules):
        fresh_rule = current_by_fp.get(fingerprint(active_rule))
        if fresh_rule is None or active_rule is not fresh_rule:
            continue
        # In production merge_rules keeps this exact fresh object. Requiring identity
        # prevents a copied/carried rule with the same guidance fingerprint but
        # different why/evidence from borrowing current-run provenance.
        focus = _spotlight(active_rule, corpus)
        if focus is None:
            continue
        choices.append((
            (
                focus.session_count,
                focus.project_count,
                focus.day_count,
                -active_index,
            ),
            focus,
        ))

    return max(choices, key=lambda item: item[0])[1] if choices else None
