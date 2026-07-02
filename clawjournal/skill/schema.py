"""Typed shapes + validation for distilled skills (Mode A).

A distilled artifact is a *small* set (<=5) of ``SkillRule``s. Each rule is either
an ``avoid`` (from a recurring failure mode) or a ``do`` (from a recurring
success/recovery). The LLM distill step returns JSON validated against
``SKILL_DISTILL_SCHEMA``; ``parse_rules`` normalizes + caps it.

``find_external_tokens`` is the deterministic prompt-injection hard-deny: a rule
that references a URL, an out-of-repo path, a literal shell command, a secret-like
token, or a tool/MCP id is rejected before it can be installed into agent context.
(Human review is the binding control; this is the deterministic backstop.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Hard cap. Too many skills are unmanageable; 5 keeps review/use/maintenance sane.
MAX_RULES = 5

VALID_KINDS = ("avoid", "do")

# The judge's fixed failure-mode taxonomy (12 + 1 meta), reused so the distill
# prompt and any per-rule ``taxonomy`` tag stay aligned with scoring.
FAILURE_MODES = (
    "task_framing", "method_selection", "context_handling", "execution_error",
    "reasoning_fabrication", "revision_failure", "verification_skipped",
    "deliverable_defect", "communication_error", "collaboration_error",
    "safety_security", "efficiency_waste", "evaluation_measurement",
)


@dataclass
class SkillRule:
    kind: str               # "avoid" | "do"
    trigger: str            # when this applies ("when you are about to ...")
    guidance: str           # the rule itself ("don't X" / "do Y instead")
    why: str                # one line on why, grounded in the user's own sessions
    title: str = ""         # short human name for the rule (the heading)
    evidence_session_ids: list[str] = field(default_factory=list)
    taxonomy: str = ""      # the failure mode it targets (avoid) or "" (do)
    support: int = 0        # how many sessions this pattern recurred in
    last_seen: str = ""     # ISO ts a stored rule was last seen ("" = seen this run)

    def display_title(self) -> str:
        """The heading to render; falls back to guidance when unnamed."""
        return self.title.strip() or _derive_title(self.guidance)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "trigger": self.trigger, "guidance": self.guidance,
            "why": self.why, "title": self.title,
            "evidence_session_ids": list(self.evidence_session_ids),
            "taxonomy": self.taxonomy, "support": self.support,
        }


# JSON schema handed to the agent CLI (Codex --output-schema; also documents the
# shape for Claude). Kept small and strict.
SKILL_DISTILL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["rules"],
    "properties": {
        "rules": {
            "type": "array",
            "maxItems": MAX_RULES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                # Codex strict --output-schema requires EVERY property of an
                # additionalProperties:false object to be listed here, or it rejects
                # the schema and the default Codex distill silently returns nothing.
                "required": ["kind", "title", "trigger", "guidance", "why",
                             "taxonomy", "evidence_session_ids"],
                "properties": {
                    "kind": {"type": "string", "enum": list(VALID_KINDS)},
                    "title": {"type": "string"},
                    "trigger": {"type": "string"},
                    "guidance": {"type": "string"},
                    "why": {"type": "string"},
                    "taxonomy": {"type": "string"},
                    "evidence_session_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}


def _coerce_support(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _derive_title(guidance: str) -> str:
    """Fallback short name from the rule text when the model omits a title."""
    words = re.split(r"\s+", (guidance or "").strip())
    return " ".join(words[:4]).rstrip(".,;:—- ")


def parse_rules(data: dict[str, Any]) -> list[SkillRule]:
    """Validate + normalize the distill output into <=MAX_RULES SkillRules.

    Tolerant of extra keys / missing optionals; drops malformed rules; caps the
    list. A rule with no trigger+guidance is dropped (nothing to teach).
    """
    raw = (data or {}).get("rules") or []
    rules: list[SkillRule] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip().lower()
        if kind not in VALID_KINDS:
            # Infer from taxonomy: a named failure mode -> avoid, else do.
            kind = "avoid" if str(item.get("taxonomy", "")).strip() in FAILURE_MODES else "do"
        trigger = str(item.get("trigger", "")).strip()
        guidance = str(item.get("guidance", "")).strip()
        if not trigger or not guidance:
            continue
        ev = item.get("evidence_session_ids") or []
        ev = [str(x).strip() for x in ev if str(x).strip()] if isinstance(ev, list) else []
        taxonomy = str(item.get("taxonomy", "")).strip()
        if taxonomy not in FAILURE_MODES:
            taxonomy = ""
        title = str(item.get("title", "")).strip() or _derive_title(guidance)
        rules.append(SkillRule(
            kind=kind,
            trigger=trigger,
            guidance=guidance,
            why=str(item.get("why", "")).strip(),
            title=title,
            evidence_session_ids=ev,
            taxonomy=taxonomy,
            support=_coerce_support(item.get("support", 0)),
        ))
        if len(rules) >= MAX_RULES:
            break
    return rules


# --- deterministic hard-deny (prompt-injection backstop) --------------------

_URL_RE = re.compile(r"\b(?:https?|ftp)://\S+|\bwww\.\S+\.\w", re.I)
# Out-of-repo ABSOLUTE paths / specific credential files. NB: we deliberately do NOT
# deny bare dotfile names (.env / .ssh) — a lesson like "keep secrets in .env, never
# commit it" is legitimate advice, and the anonymizer already strips real home paths.
_OUT_OF_REPO_PATH_RE = re.compile(
    r"(?:^|\s)(?:/Users/|/home/|/etc/|/var/|/root/|~/\.|[A-Za-z]:\\)\S+|\.aws/credentials", re.I
)
# EXECUTABLE shell syntax only — command substitution ($(...)) and pipe-to-shell.
# We do NOT deny bare command NAMES (eval/sudo/rm -rf/curl) or markdown backtick code
# spans (`rg`, `npm run build`): those are advisory formatting/mentions, and a rule is
# inert text in agent context — only actionable injection syntax is a real risk.
_SHELL_META_RE = re.compile(r"\$\([^)]+\)|\|\s*(?:sh|bash)\b")
# tool / MCP ids the agent could be steered to call (not the phrase "tool call").
_TOOL_ID_RE = re.compile(r"\bmcp__\w+", re.I)
# High-confidence secret PREFIXES only. Bare 40-hex / 40-base64 runs are dropped from
# this per-rule deny because they false-positive on git commit SHAs and hashes that
# grounded lessons legitimately cite; real high-entropy secrets are caught upstream by
# _scrub (before distill) and by the render-time secrets/TruffleHog gate.
_SECRET_RE = re.compile(
    r"\b(?:AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_\-]{20,})\b"
)

_DENY = [
    ("url", _URL_RE), ("out_of_repo_path", _OUT_OF_REPO_PATH_RE),
    ("shell_meta", _SHELL_META_RE),
    ("tool_id", _TOOL_ID_RE), ("secret_like", _SECRET_RE),
]


def find_external_tokens(rule: SkillRule) -> list[str]:
    """Return reasons a rule must be hard-denied (empty list = clean).

    Scans the agent-facing fields (trigger/guidance/why) for concrete external or
    executable tokens. Ordinary in-repo references (``run the test suite``,
    ``src/foo.py``) do NOT match.
    """
    text = "\n".join([
        rule.title,
        rule.trigger,
        rule.guidance,
        rule.why,
        rule.taxonomy,
        *rule.evidence_session_ids,
    ])
    hits: list[str] = []
    for label, rx in _DENY:
        if rx.search(text):
            hits.append(label)
    return hits
