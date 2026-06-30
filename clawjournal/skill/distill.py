"""The one LLM step: distill selected candidates into <=5 skill rules.

All substrate is anonymized (home/username) AND deterministically secrets-scrubbed
*before* the call — the only AI egress in default Mode A is this single call,
through the user's own agent CLI (``ANTHROPIC_API_KEY`` stripped → subscription).
The backend is reached through a ``caller`` seam so tests inject a fake; the
default mirrors the benchmark's ``AgentBackendCaller``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Protocol

from ..benchmark.generate import _extract_json_object, _read_agent_output
from ..config import load_config
from ..redaction.anonymizer import Anonymizer
from ..redaction.secrets import redact_text
from ..scoring.backends import (
    default_model_for_backend,
    resolve_backend,
    run_default_agent_task,
)
from .schema import FAILURE_MODES, MAX_RULES, SkillRule, parse_rules
from .select import SkillCorpus

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
    "coincidental routine. Ground each rule in the given sessions and list their ids "
    "in evidence_session_ids. De-identify: never put a person's name, email, URL, "
    "home path, secret, or shell command into a rule — write portable guidance. "
    f"Failure taxonomy for the 'taxonomy' field: {', '.join(FAILURE_MODES)}."
)

_RULE_SHAPE = (
    'Return JSON: {"rules": [{"kind": "avoid"|"do", "trigger": "when this applies", '
    '"guidance": "the rule (avoid X / do Y instead)", "why": "one line, grounded", '
    '"taxonomy": "<failure mode or empty>", "evidence_session_ids": ["..."]}]}'
)


def _scrub(value: Any, anon: Anonymizer) -> str:
    """Anonymize home/username then deterministically redact secrets."""
    if not value:
        return ""
    text = anon.text(str(value))
    redacted, _, _ = redact_text(text)
    return redacted


def _format_candidates(corpus: SkillCorpus, anon: Anonymizer) -> str:
    lines: list[str] = []
    if corpus.mode_recurrence:
        top = ", ".join(f"{m}×{n}" for m, n in list(corpus.mode_recurrence.items())[:8])
        lines.append(f"Recurring failure modes this window: {top}\n")
    for c in corpus.candidates:
        lines.append(
            f"- session {c.session_id} [{c.kind}] source={c.source} "
            f"project={_scrub(c.project, anon)}\n"
            f"  failure_modes={c.failure_modes} recovery={c.recovery_labels} "
            f"resolution={c.resolution} failure_value={c.failure_value} quality={c.quality}\n"
            f"  title={_scrub(c.title, anon)}\n"
            f"  learning_summary={_scrub(c.learning_summary, anon)}\n"
            f"  score_reason={_scrub(c.score_reason, anon)}"
        )
    return "\n".join(lines)


def build_prompt(corpus: SkillCorpus, anon: Anonymizer) -> str:
    return (
        "# Distill up to 5 durable skills from this user's own scored sessions.\n\n"
        f"{_format_candidates(corpus, anon)}\n\n{_RULE_SHAPE}"
    )


class DefaultCaller:
    """Production caller over ``run_default_agent_task`` (one call, no CI)."""

    def __init__(self, backend: str = "auto", model: str | None = None,
                 timeout_seconds: int = DISTILL_TIMEOUT) -> None:
        self.resolved = resolve_backend(backend)
        self.model = model or default_model_for_backend(self.resolved)
        self.timeout_seconds = timeout_seconds

    def __call__(self, *, system_prompt: str, task_prompt: str) -> dict[str, Any]:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            sys_file = cwd / "system.md"
            sys_file.write_text(system_prompt, encoding="utf-8")
            task = task_prompt if self.resolved == "claude" else f"{system_prompt}\n\n{task_prompt}"
            from .schema import SKILL_DISTILL_SCHEMA
            result = run_default_agent_task(
                backend=self.resolved, cwd=cwd, system_prompt_file=sys_file,
                task_prompt=task, model=self.model, timeout_seconds=self.timeout_seconds,
                codex_sandbox="read-only", codex_output_schema=SKILL_DISTILL_SCHEMA,
                codex_output_file="out.json", openclaw_message=task,
            )
            return _read_agent_output(self.resolved, result.stdout, cwd / "out.json")


def distill_skills(
    corpus: SkillCorpus,
    *,
    backend: str = "auto",
    model: str | None = None,
    caller: Caller | None = None,
) -> list[SkillRule]:
    """Run the single distill call and return <=MAX_RULES validated SkillRules.

    ``caller`` is injected in tests; default hits the user's own agent CLI.
    """
    if corpus.is_empty():
        return []
    anon = Anonymizer(extra_usernames=list(load_config().get("redact_usernames", []) or []))
    task = build_prompt(corpus, anon)
    call = caller or DefaultCaller(backend=backend, model=model)
    try:
        data = call(system_prompt=_SYSTEM, task_prompt=task)
    except Exception as exc:  # backend/timeout/parse failure -> degrade gracefully
        logger.warning("skill distill call failed: %s", exc)
        return []
    if not isinstance(data, dict):
        try:
            data = _extract_json_object(str(data))
        except ValueError:
            return []
    rules = parse_rules(data)
    # backfill support from recurrence where the rule named a mode
    for r in rules:
        if r.taxonomy and r.taxonomy in corpus.mode_recurrence:
            r.support = max(r.support, corpus.mode_recurrence[r.taxonomy])
    return rules
