"""Turn-grounding: extract pivotal user-correction turns from raw session traces.

Summary-grounded distillation produces generic lessons; the actual mistake →
user-correction → fix exchange is the highest-signal evidence a trace holds.
This module finds those moments heuristically (no LLM) and attaches compact
excerpts to the already-selected, already-egress-gated candidates so the one
distill call can ground its rules in what really happened.

Excerpts are extracted RAW here (local-only); they pass through the same
``_scrub`` (anonymize + deterministic secret redaction) as every other field
when ``distill._format_candidates`` builds the prompt.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from ..scoring.scoring import extract_tool_uses, get_message_text

logger = logging.getLogger(__name__)

MAX_EXCERPTS_PER_SESSION = 2
_BEFORE_CHARS = 350   # tail of the agent's last claim (the claim sits at the end)
_CORRECTION_CHARS = 350
_AFTER_CHARS = 250
_SIGNAL_SCAN_CHARS = 600  # corrections lead with the redirect; a match buried deep
                          # in a long message is usually a new task spec, not a correction

# Strong correction signals anywhere in the head of the message.
_STRONG_RE = re.compile(
    r"that'?s not|that is not|not what i (?:asked|meant|want)|i asked (?:for|you)|"
    r"you (?:missed|forgot|didn'?t|should have)|still (?:fail|break|broken|wrong|not work)"
    r"|(?:doesn'?t|didn'?t|does not|did not) work|\brevert\b|undo (?:that|this)|"
    r"\bwrong\b|\binstead\b|re-?read|fix it in|"
    r"fix (?:it|this|that) (?:properly|correctly|right|again)|try again|\bredo\b",
    re.I,
)
# Weaker signals that only count when they OPEN the message.
_LEAD_RE = re.compile(
    r"^(?:no\b|nope\b|wait\b|stop\b|actually\b|hmm+,? no|don'?t\b|not\b|wrong\b)",
    re.I,
)

_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
# Harness/command wrappers that reach us with role=user but aren't the human talking.
_NOISE_PREFIXES = ("<command-", "<local-command", "<task-notification", "Caveat:",
                   "[Request interrupted")
# Harness-INJECTED bodies with no wrapper at all: expanded skill/slash-command content
# (carries an "Invoke: ..." contract line) and compaction summaries. Their imperative
# phrasing ("do X instead") reads as a correction but isn't the human talking.
_INJECTED_RE = re.compile(
    r"^Invoke: |<command-name>|^Base directory for this skill|"
    r"^This session is being continued from a previous conversation",
    re.M,
)


@dataclass
class TurnExcerpt:
    before: str      # what the agent had just claimed/done
    correction: str  # the user's actual redirect
    after: str       # how the agent responded (the fix)


def _clean_user_text(msg: dict) -> str:
    text = _SYSTEM_REMINDER_RE.sub("", get_message_text(msg)).strip()
    if not text or text.startswith(_NOISE_PREFIXES) or _INJECTED_RE.search(text):
        return ""
    return text


def is_correction(text: str) -> bool:
    head = text[:_SIGNAL_SCAN_CHARS]
    return bool(_LEAD_RE.match(head) or _STRONG_RE.search(head))


def _head(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tail(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else "…" + text[-(limit - 1):]


def _agent_activity(msgs: list[dict], *, last: bool) -> str:
    """The agent's text in a span of assistant messages; fall back to tool names."""
    texts = [t for m in msgs if m.get("role") == "assistant"
             for t in [get_message_text(m).strip()] if t]
    if texts:
        return texts[-1] if last else texts[0]
    tools = list(dict.fromkeys(
        tu.get("tool", "") for m in msgs if m.get("role") == "assistant"
        for tu in extract_tool_uses(m) if tu.get("tool")))
    return f"(tools: {', '.join(tools)})" if tools else ""


def extract_correction_turns(
    messages: list[dict],
    *,
    max_excerpts: int = MAX_EXCERPTS_PER_SESSION,
) -> list[TurnExcerpt]:
    """Find user-correction turns and return compact before/correction/after excerpts.

    The FIRST real user message is the task, never a correction. ``before`` is the
    agent's activity since the previous user message (its claim); ``after`` is its
    activity up to the next user message (the fix).
    """
    excerpts: list[TurnExcerpt] = []
    user_seen = False
    for i, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        text = _clean_user_text(msg)
        if not text:
            continue
        if not user_seen:          # the initial task
            user_seen = True
            continue
        if not is_correction(text):
            continue
        # span back to the previous user message, forward to the next one
        start = next((j for j in range(i - 1, -1, -1)
                      if messages[j].get("role") == "user"), -1) + 1
        end = next((j for j in range(i + 1, len(messages))
                    if messages[j].get("role") == "user"), len(messages))
        before = _agent_activity(messages[start:i], last=True)
        after = _agent_activity(messages[i + 1:end], last=False)
        if not before:             # a "correction" of nothing is noise
            continue
        excerpts.append(TurnExcerpt(
            before=_tail(before, _BEFORE_CHARS),
            correction=_head(text, _CORRECTION_CHARS),
            after=_head(after, _AFTER_CHARS),
        ))
        if len(excerpts) >= max_excerpts:
            break
    return excerpts


def excerpts_for_session(
    conn: Any,
    session_id: str,
    *,
    max_excerpts: int = MAX_EXCERPTS_PER_SESSION,
    loader: Callable[[Any, str], dict | None] | None = None,
) -> list[TurnExcerpt]:
    """Best-effort pivotal-turn excerpts for one session (empty on any failure).

    Injected into ``select_skill_candidates`` as its ``excerpt_loader`` so a captured
    correction both boosts the candidate's rank and rides along to the distill prompt.
    Callers must only pass ALREADY-gated session ids (hold-state + excluded-project
    checks happen in selection), so nothing new can leak into that prompt.
    """
    if loader is None:
        from ..workbench.index import get_session_detail
        loader = get_session_detail
    try:
        detail = loader(conn, session_id)
        messages = (detail or {}).get("messages") or []
        return extract_correction_turns(messages, max_excerpts=max_excerpts)
    except Exception as exc:  # a bad blob must never sink the run
        logger.warning("turn extraction failed for %s: %s", session_id, exc)
        return []
