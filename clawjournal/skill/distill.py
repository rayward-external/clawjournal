"""The one LLM step: distill selected candidates into <=5 skill rules.

All substrate is anonymized (home/username) AND deterministically secrets-scrubbed
*before* the call — the only AI egress in default Mode A is this single call,
through the user's own agent CLI (``ANTHROPIC_API_KEY`` stripped → subscription).
The backend is reached through a ``caller`` seam so tests inject a fake; the
default mirrors the benchmark's ``AgentBackendCaller``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Protocol

from ..benchmark.generate import _extract_json_object, run_agent_json_call
from ..config import load_config
from ..redaction.anonymizer import Anonymizer
from ..redaction.secrets import redact_text
from ..scoring.backends import (
    default_distill_effort_for_backend,
    default_distill_model_for_backend,
    default_model_for_backend,
    is_backend_unavailable_error,
    resolve_backend,
)
from .schema import FAILURE_MODES, MAX_RULES, SkillRule, parse_rules
from .select import SkillCorpus

# A distill failure caused by an OLD agent CLI that doesn't recognize the newer
# --safe-mode / --effort flags (vs a transient timeout or a plan-unavailable model).
_DISTILL_FLAG_ERROR_RE = re.compile(
    r"safe-?mode|--effort|unknown (?:option|flag|argument)|unrecognized|unexpected argument",
    re.I,
)


def _distill_flag_unsupported(message: str) -> bool:
    return bool(_DISTILL_FLAG_ERROR_RE.search(message or ""))

logger = logging.getLogger(__name__)

DISTILL_TIMEOUT = 240


class Caller(Protocol):
    def __call__(self, *, system_prompt: str, task_prompt: str) -> dict[str, Any]:
        ...


_SYSTEM = (
    "You distill a SMALL set of durable, reusable coding-agent skills from a single "
    "user's OWN past sessions (already scored). Output ONLY one JSON object, no prose, "
    f"no markdown fences. Produce AT MOST {MAX_RULES} rules total — a mix of 'avoid' "
    "(from recurring failures) and 'do' (from recurring successes/recoveries). "
    "Pick the highest-value RECURRING, NONTRIVIAL patterns; ignore one-off or trivial "
    "wins. A success is not *caused* by a named habit — for 'do' rules, teach the "
    "specific fix/decision that turned a failure into a success (the delta), not a "
    "coincidental routine. "
    "PRIORITIZE BY RECURRENCE AND BREADTH — this skill loads on EVERY coding task, so it "
    "is worth most when its rules fire often. Favor the failure modes with the highest "
    "recurrence counts (listed below) and habits that apply to MANY of the user's tasks "
    "over a rare-but-severe one-off incident. A durable everyday habit ('before calling "
    "it done, re-run the exact failing case you were asked to fix and confirm it passes') "
    "beats a narrow post-mortem tied to a single project/value. Keep guidance concrete "
    "and checkable, but choose broadly-applicable patterns. "
    "BE SPECIFIC, NOT GENERIC: each rule must name the concrete situation, surface, or "
    "pattern it targets and keep the technical detail of the source learning_summary "
    "(e.g. 'schema/prompt changes are a cross-surface contract — verify DB, API, UI, and "
    "exports together', 'when a safety gate blocks a multi-item batch, support auditable "
    "per-item removal and retry, not all-or-nothing', 'run install + runtime smoke tests "
    "against the built artifact before tagging a release'). REJECT vague platitudes "
    "('write tests', 'verify your work', 'check the environment', 'communicate clearly') "
    "unless bound to a concrete trigger and a checkable mechanism. The 'trigger' is a "
    "recognizable situation the user will hit again; the 'guidance' is one concrete, "
    "checkable action. Ground each rule in the given sessions and list their case ids in "
    "evidence_session_ids. "
    "Give each rule a 'title': a very short name of 2-4 words (aim for 2-3) that reads "
    "like a command/skill name (e.g. 'Validate config types', 'Verify patch applied', "
    "'Recompute final badge', 'Guard script secrets'). Title Case, no trailing period, "
    "not a sentence; distinct from the longer 'guidance'. "
    "De-identify PII ONLY — never emit a person's name, email, URL, "
    "home path, secret, or verbatim shell command — but KEEP technical specifics (repo and "
    "module names, failure surfaces, tool categories, architectural patterns). "
    f"Failure taxonomy for the 'taxonomy' field: {', '.join(FAILURE_MODES)}."
)

_RULE_SHAPE = (
    'Return JSON: {"rules": [{"kind": "avoid"|"do", '
    '"title": "2-4 word name", "trigger": "when this applies", '
    '"guidance": "the rule (avoid X / do Y instead)", "why": "one line, grounded", '
    '"taxonomy": "<failure mode or empty>", "evidence_session_ids": ["case-01"]}]}'
)


def _scrub(value: Any, anon: Anonymizer) -> str:
    """Anonymize home/username then deterministically redact secrets."""
    if not value:
        return ""
    text = anon.text(str(value))
    redacted, _, _ = redact_text(text)
    return redacted


def _candidate_aliases(corpus: SkillCorpus) -> dict[str, str]:
    return {c.session_id: f"case-{i:02d}" for i, c in enumerate(corpus.candidates, 1)}


def _format_candidates(corpus: SkillCorpus, anon: Anonymizer, aliases: dict[str, str]) -> str:
    lines: list[str] = []
    if corpus.mode_recurrence:
        top = ", ".join(f"{m}×{n}" for m, n in list(corpus.mode_recurrence.items())[:8])
        lines.append(f"Recurring failure modes this window: {top}\n")
    for c in corpus.candidates:
        lines.append(
            f"- session {aliases[c.session_id]} [{c.kind}] source={c.source} "
            f"project={_scrub(c.project, anon)}\n"
            f"  support_count={c.support_count} impact={c.impact:.2f} "
            f"recency={c.recency:.3f} rank_score={c.rank_score:.3f}\n"
            f"  failure_modes={c.failure_modes} recovery={c.recovery_labels} "
            f"resolution={c.resolution} failure_value={c.failure_value} quality={c.quality}\n"
            f"  title={_scrub(c.title, anon)}\n"
            f"  learning_summary={_scrub(c.learning_summary, anon)}\n"
            f"  score_reason={_scrub(c.score_reason, anon)}"
        )
    return "\n".join(lines)


def build_prompt(corpus: SkillCorpus, anon: Anonymizer, aliases: dict[str, str]) -> str:
    return (
        "# Distill up to 5 durable skills from this user's own scored sessions.\n\n"
        f"{_format_candidates(corpus, anon, aliases)}\n\n{_RULE_SHAPE}"
    )


class DefaultCaller:
    """Production caller over ``run_default_agent_task`` (one call, no CI)."""

    def __init__(self, backend: str = "auto", model: str | None = None,
                 effort: str | None = None,
                 timeout_seconds: int = DISTILL_TIMEOUT,
                 use_default_distill_effort: bool = True,
                 claude_safe_mode: bool = True) -> None:
        self.resolved = resolve_backend(backend)
        self.claude_safe_mode = claude_safe_mode
        # Distill defaults to a frontier model and higher effort; explicit CLI
        # overrides still win. Scoring defaults live in scoring.backends.
        self.model = model or default_distill_model_for_backend(self.resolved)
        self.effort = (
            effort if effort is not None
            else (default_distill_effort_for_backend(self.resolved)
                  if use_default_distill_effort else None)
        )
        self.timeout_seconds = timeout_seconds

    def __call__(self, *, system_prompt: str, task_prompt: str) -> dict[str, Any]:
        from .schema import SKILL_DISTILL_SCHEMA
        # claude_safe_mode -> Claude Code runs WITHOUT auto-loading the installed
        # clawjournal-lessons skill / user CLAUDE.md, while still using normal auth.
        # That keeps distillation grounded only in the passed corpus and prevents
        # already-installed or --rejected rules from being re-emitted/reinforced.
        return run_agent_json_call(
            resolved=self.resolved, model=self.model, effort=self.effort,
            system_prompt=system_prompt, task_prompt=task_prompt,
            timeout_seconds=self.timeout_seconds,
            codex_output_schema=SKILL_DISTILL_SCHEMA,
            claude_safe_mode=self.claude_safe_mode,
        )


def distill_skills(
    corpus: SkillCorpus,
    *,
    backend: str = "auto",
    model: str | None = None,
    effort: str | None = None,
    caller: Caller | None = None,
    cfg: dict | None = None,
) -> list[SkillRule]:
    """Run the single distill call and return <=MAX_RULES validated SkillRules.

    ``caller`` is injected in tests; default hits the user's own agent CLI. ``cfg``
    reuses an already-loaded config (for redact_usernames) instead of re-reading it.
    """
    if corpus.is_empty():
        return []
    cfg = cfg if cfg is not None else load_config()
    anon = Anonymizer(extra_usernames=list(cfg.get("redact_usernames", []) or []))
    aliases = _candidate_aliases(corpus)  # computed once, reused for evidence back-mapping
    task = build_prompt(corpus, anon, aliases)
    try:
        # DefaultCaller() resolves the backend (a process/env lookup that can raise
        # when no backend is installed), so build it INSIDE the degrade-gracefully guard.
        call = caller or DefaultCaller(backend=backend, model=model, effort=effort)
        data = call(system_prompt=_SYSTEM, task_prompt=task)
    except Exception as exc:  # backend/timeout/parse failure -> degrade gracefully
        msg = str(exc)
        logger.warning("skill distill call failed: %s", exc)
        # Only a default (caller-less, no explicit model/effort) frontier run is eligible
        # for a graceful retry.
        if caller is not None or model is not None or effort is not None:
            return []
        plan_issue = is_backend_unavailable_error(msg)  # frontier model unavailable on the plan
        flag_issue = _distill_flag_unsupported(msg)      # old CLI rejects --safe-mode/--effort
        # A TRANSIENT failure (timeout/network/parse) must NOT silently downgrade the
        # model+effort with a misleading "model unavailable" message — degrade to [].
        if not (plan_issue or flag_issue):
            return []
        try:
            resolved = resolve_backend(backend)
        except Exception:
            return []
        frontier = default_distill_model_for_backend(resolved)  # what the 1st try used
        fast = default_model_for_backend(resolved)
        can_downgrade = plan_issue and fast and fast != frontier
        if not (can_downgrade or flag_issue):
            return []  # nothing to relax (e.g. Codex plan issue where fast IS the frontier)
        try:
            # plan issue -> drop to the fast model; flag issue -> keep the frontier model
            # but relax the newer --safe-mode/--effort flags an old CLI can't parse.
            data = DefaultCaller(
                backend=resolved,
                model=(fast if can_downgrade else None),
                use_default_distill_effort=not flag_issue,
                claude_safe_mode=not flag_issue,
            )(system_prompt=_SYSTEM, task_prompt=task)
            why = ("the frontier distill model is unavailable on your plan" if plan_issue
                   else "your agent CLI rejected the newer distill flags")
            where = f" on {fast}" if can_downgrade else ""
            print(f"note: {why}; retried with a compatible config{where}. "
                  f"Pass --model / --effort to control it.")
        except Exception:
            return []
    if not isinstance(data, dict):
        try:
            data = _extract_json_object(str(data))
        except ValueError:
            return []
    rules = parse_rules(data)
    allowed_aliases = set(aliases.values())
    # backfill support from recurrence where the rule named a mode
    for r in rules:
        evidence: list[str] = []
        for sid in r.evidence_session_ids:
            if sid in aliases:
                evidence.append(aliases[sid])
            elif sid in allowed_aliases:
                evidence.append(sid)
        r.evidence_session_ids = list(dict.fromkeys(evidence))
        if r.taxonomy and r.taxonomy in corpus.mode_recurrence:
            r.support = max(r.support, corpus.mode_recurrence[r.taxonomy])
    return rules
