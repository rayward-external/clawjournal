"""Mode A: distill a small set of self-improving skills from the user's own
scored coding-agent sessions, entirely locally.

Pipeline (all on the user's machine; only the distill step calls an LLM, via the
user's own agent CLI): select top candidates (recurring failures = "avoid this" +
recurring successes/recoveries = "do this") -> anonymize + secrets-scrub -> one
distill call -> render <=5 rules -> deterministic gate -> preview -> install a
``clawjournal-lessons`` skill for Claude Code and Codex.

See ``docs/self-improving-skills/plan.md`` (Mode A) for the design.
"""

from .schema import MAX_RULES, SkillRule

__all__ = ["MAX_RULES", "SkillRule"]
