"""Render <=5 SkillRules into the agent surfaces + the deterministic gate.

Two render targets share one body:
  - Claude Code: a full Agent Skill ``SKILL.md`` (name + description frontmatter).
  - Codex: a markdown managed-region (no frontmatter) for ``AGENTS.md``.

The gate has two deterministic layers (human review is the binding control on top):
  - ``gate_rules`` drops any rule whose text trips the external/exec hard-deny;
  - ``gate_rendered`` scans the final text for secrets/PII (and TruffleHog when
    available) and reports findings so the caller blocks the write.
"""

from __future__ import annotations

from typing import Any

from ..redaction import pii, secrets, trufflehog
from .schema import SkillRule, find_external_tokens

SKILL_NAME = "clawjournal-lessons"
SKILL_DESCRIPTION = (
    "Personal coding lessons distilled from YOUR OWN past agent sessions. Load "
    "before planning or executing a coding task to avoid recurring mistakes and "
    "repeat what worked. Triggers on coding, debugging, refactoring, or "
    "verification tasks."
)


def gate_rules(rules: list[SkillRule]) -> tuple[list[SkillRule], list[tuple[SkillRule, list[str]]]]:
    """Split rules into (kept, blocked-with-reasons) via the hard-deny."""
    kept: list[SkillRule] = []
    blocked: list[tuple[SkillRule, list[str]]] = []
    for r in rules:
        reasons = find_external_tokens(r)
        (blocked.append((r, reasons)) if reasons else kept.append(r))
    return kept, blocked


def gate_secret_pii_per_rule(
    rules: list[SkillRule], *, run_trufflehog: bool = False,
) -> tuple[list[SkillRule], list[tuple[SkillRule, list[str]]]]:
    """Split rules by the secret/PII/(optional TruffleHog) scan applied PER RULE.

    A single dirty rule is DROPPED (moved to blocked-with-reasons) rather than left to
    fail the whole-document ``gate_rendered`` — which would dead-end the install with a
    fingerprint the user can't ``--reject`` (gate-failed runs persist nothing). The
    default skips the TruffleHog subprocess (fast regex only); the caller re-runs with
    ``run_trufflehog=True`` to pinpoint a detector-only finding the whole-doc pass caught.
    """
    kept: list[SkillRule] = []
    blocked: list[tuple[SkillRule, list[str]]] = []
    for r in rules:
        text = "\n".join([r.title, r.trigger, r.guidance, r.why, *r.evidence_session_ids])
        issues = gate_rendered(text, run_trufflehog=run_trufflehog)
        (blocked.append((r, issues)) if issues else kept.append(r))
    return kept, blocked


def gate_has_scanner_error(issues: list[str]) -> bool:
    """True if any gate issue is a scanner INFRA error rather than a content finding.

    Colocated with ``gate_rendered`` so the classification stays in lock-step with the
    message format it emits (rather than the caller sniffing prose across a module
    boundary): a CONTENT finding is always formatted with a "match(es)"/"finding(s)"
    count; anything else (missing binary, timeout, crash, scan failure) is infra.
    """
    return any(("match(es)" not in i) and ("finding(s)" not in i) for i in issues)


def gate_rendered(text: str, *, run_trufflehog: bool = True) -> list[str]:
    """Return deterministic secret/PII/TruffleHog findings in *text* (empty = clean)."""
    issues: list[str] = []
    try:
        n = len(secrets.scan_text(text))
        if n:
            issues.append(f"secrets: {n} match(es)")
    except Exception as exc:  # pragma: no cover - defensive
        issues.append(f"secrets: scan failed ({exc.__class__.__name__})")
    try:
        n = len(pii.scan_text_for_pii(text))
        if n:
            issues.append(f"pii: {n} match(es)")
    except Exception as exc:  # pragma: no cover
        issues.append(f"pii: scan failed ({exc.__class__.__name__})")
    try:
        if run_trufflehog and not trufflehog.is_bypassed():
            report = trufflehog.scan_text(text)
            findings = getattr(report, "findings", None) or []
            if getattr(report, "blocking", False):
                reason = getattr(report, "block_reason", None) or "trufflehog-error"
                issues.append(f"trufflehog: {len(findings)} finding(s)")
                if not findings:
                    issues[-1] = f"trufflehog: {reason}"
    except Exception as exc:  # pragma: no cover
        issues.append(f"trufflehog: scan failed ({exc.__class__.__name__})")
    return issues


def _render_body(rules: list[SkillRule], meta: dict[str, Any]) -> str:
    avoid = [r for r in rules if r.kind == "avoid"]
    do = [r for r in rules if r.kind == "do"]
    out: list[str] = [
        "# Your coding lessons",
        "",
        "> Distilled by ClawJournal from your own scored sessions. Local-only; "
        "regenerated as you keep working. Each is grounded in real sessions of yours.",
        "",
    ]

    def block(r: SkillRule) -> list[str]:
        heading = r.display_title()
        lines = [f"### {heading}"]
        if r.guidance and r.guidance.strip() != heading:
            lines.append(f"- **Rule:** {r.guidance}")
        if r.trigger:
            lines.append(f"- **When:** {r.trigger}")
        if r.why:
            lines.append(f"- **Why:** {r.why}")
        tags = []
        if r.taxonomy:
            tags.append(r.taxonomy)
        if r.support:
            tags.append(f"seen ~{r.support}×")
        # NB: we deliberately do NOT render evidence_session_ids — they are ephemeral
        # per-run 'case-NN' aliases, and a carried-over rule would cite case ids that map
        # to unrelated sessions this run (misleading provenance in the installed file).
        if tags:
            lines.append(f"- _{' · '.join(tags)}_")
        return lines

    if avoid:
        out.append("## Avoid (recurring failures)")
        out.append("")
        for r in avoid:
            out += block(r) + [""]
    if do:
        out.append("## Do (what worked)")
        out.append("")
        for r in do:
            out += block(r) + [""]

    prov = "; ".join(f"{k}={v}" for k, v in meta.items())
    out.append(f"<!-- clawjournal-lessons: {prov} -->")
    return "\n".join(out).rstrip() + "\n"


def render_skill_md(rules: list[SkillRule], meta: dict[str, Any]) -> str:
    """Full Claude Agent Skill SKILL.md (frontmatter + body)."""
    body = _render_body(rules, meta)
    return f"---\nname: {SKILL_NAME}\ndescription: {SKILL_DESCRIPTION}\n---\n\n{body}"


def render_agents_region(rules: list[SkillRule], meta: dict[str, Any]) -> str:
    """Body markdown for the Codex AGENTS.md managed region (no frontmatter)."""
    return _render_body(rules, meta)


def render_targets(rules: list[SkillRule], meta: dict[str, Any]) -> tuple[str, str]:
    """Build the shared body ONCE and return (Claude SKILL.md, Codex region).

    The Claude and Codex outputs share the identical body, so callers use this to avoid
    assembling it twice (or four times on the TruffleHog-pinpoint re-render path).
    """
    body = _render_body(rules, meta)
    skill_md = f"---\nname: {SKILL_NAME}\ndescription: {SKILL_DESCRIPTION}\n---\n\n{body}"
    return skill_md, body
