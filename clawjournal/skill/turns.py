"""Turn-grounding: extract pivotal feedback turns from raw session traces.

Summary-grounded distillation produces generic lessons; the highest-signal
evidence a trace holds is OBJECTIVE feedback, not the judge's opinion:

- HUMAN feedback — the mistake → user-correction → fix exchange;
- ENVIRONMENT feedback — a tool call that errored, and the changed call that
  then worked (plus the same error signature recurring across many sessions).

This module finds both heuristically (no LLM) and attaches compact excerpts to
the already-selected, already-egress-gated candidates so the one distill call
can ground its rules in what really happened.

Excerpts are extracted RAW here (local-only); they pass through the same
``_scrub`` (anonymize + custom redactions + deterministic secret redaction) as
every other field when ``distill._format_candidates`` builds the prompt.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ..scoring.scoring import extract_tool_uses, get_message_text

logger = logging.getLogger(__name__)

MAX_EXCERPTS_PER_SESSION = 2       # user-correction excerpts per session
MAX_ENV_EXCERPTS_PER_SESSION = 2   # error->recovery excerpts per session
MAX_EXCERPTS_TOTAL = 3             # combined cap fed to the distill prompt
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
    r"\bwrong\b|re-?read|fix it in|"
    r"fix (?:it|this|that) (?:properly|correctly|right|again)|try again|\bredo\b",
    re.I,
)
# Weaker signals that only count when they OPEN the message.
_LEAD_RE = re.compile(
    r"^(?:no\b|nope\b|wait\b|stop\b|actually\b|hmm+,? no|don'?t\b|not\b|wrong\b)",
    re.I,
)
# "…instead…" is a redirect in a DECLARATIVE message ("let's ground by raw traces
# instead") but just an option being weighed in a QUESTION ("can we use gpt as the
# judge instead?") — real-corpus false positives were all interrogative.
_INSTEAD_RE = re.compile(r"\binstead\b", re.I)
_QUESTION_HEAD_RE = re.compile(
    r"^(?:another question|how\b|what\b|can (?:we|you|i)\b|could\b|should\b|would\b|"
    r"is\b|are\b|do\b|does\b|did\b|why\b|when\b|where\b|which\b|who\b)",
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
    r"^This session is being continued from a previous conversation|"
    r"<teammate-message",   # relayed from ANOTHER agent session, not this session's human
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
    if _LEAD_RE.match(head) or _STRONG_RE.search(head):
        return True
    return bool(_INSTEAD_RE.search(head)) and not _QUESTION_HEAD_RE.match(head)


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


# --- environment feedback (tool errors + recoveries) ------------------------

@dataclass
class EnvExcerpt:
    action: str      # the failing tool call (tool + input head)
    error: str       # what the environment said
    recovery: str    # the changed call that then worked ("" if it never did)


_ACTION_CHARS = 160
_ERROR_CHARS = 240

_PATH_RE = re.compile(r"(/[\w.\-]+){2,}")
_HEX_RE = re.compile(r"\b[0-9a-f]{7,}\b")
_NUM_RE = re.compile(r"\d+")

# Routine navigation/probing errors — grepping a path that may not exist is normal
# work, not a lesson.
_BENIGN_ERROR_RE = re.compile(
    r"no matches found|file does not exist|is a directory\b|eisdir|"
    r"shorter than the provided offset",
    re.I,
)
# Human rejections arrive AS tool errors (permission denials, interrupts). They are
# human feedback, not environment feedback — never teach them as an env pitfall.
_HUMAN_REJECTION_RE = re.compile(
    r"permission (?:to use|for this action)|denied by the|"
    r"doesn'?t want to take this action|request interrupted|rejected the tool",
    re.I,
)


def _error_text(output: Any) -> str:
    if isinstance(output, dict):
        return str(output.get("text", "") or "")
    return str(output or "")


def error_signature(output: Any) -> str:
    """Normalized first INFORMATIVE line of an error output, for cross-session
    clustering (paths/hex/numbers collapsed). '' if nothing distinctive enough —
    a bare 'exit code 1' aggregates unrelated failures and must not cluster."""
    for line in _error_text(output).splitlines():
        line = line.strip()
        if not line:
            continue
        norm = _NUM_RE.sub("#", _HEX_RE.sub("<hex>", _PATH_RE.sub("<path>", line))).lower()
        if norm in ("exit code #", "error", "error:", "failed") or len(norm) < 8:
            continue  # too generic; try the next line
        # traceback preambles/frames aggregate ALL python errors into one cluster;
        # the informative line (e.g. `KeyError: 'x'`) comes after them.
        if norm.startswith(("traceback (most recent call last)", 'file "')):
            continue
        return norm[:110]
    return ""


def _teachable_error(tu: dict) -> str:
    """The error signature if this failed tool call is worth learning from."""
    err = _error_text(tu.get("output"))
    if _HUMAN_REJECTION_RE.search(err) or _BENIGN_ERROR_RE.search(err):
        return ""
    return error_signature(tu.get("output"))


def _action_desc(tu: dict) -> str:
    inp = tu.get("input")
    arg = ""
    if isinstance(inp, dict):
        for v in inp.values():
            if isinstance(v, str) and v.strip():
                arg = v.strip()
                break
    elif isinstance(inp, str):
        arg = inp.strip()
    tool = tu.get("tool", "?")
    return f"{tool}: {arg}" if arg else tool


def extract_error_recoveries(
    messages: list[dict],
    *,
    max_excerpts: int = MAX_ENV_EXCERPTS_PER_SESSION,
) -> list[EnvExcerpt]:
    """Error -> later success on the SAME tool: the objective 'what worked' delta."""
    pending: dict[str, dict] = {}   # tool -> latest teachable failing call
    out: list[EnvExcerpt] = []
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tu in extract_tool_uses(m):
            tool = tu.get("tool", "")
            if tu.get("status") == "error":
                if _teachable_error(tu):
                    pending[tool] = tu
            elif tool in pending:
                failing = pending.pop(tool)
                out.append(EnvExcerpt(
                    action=_head(_action_desc(failing), _ACTION_CHARS),
                    error=_head(_error_text(failing.get("output")), _ERROR_CHARS),
                    recovery=_head(_action_desc(tu), _ACTION_CHARS),
                ))
                if len(out) >= max_excerpts:
                    return out
    return out


def excerpts_for_session(
    conn: Any,
    session_id: str,
    *,
    max_excerpts: int = MAX_EXCERPTS_TOTAL,
    loader: Callable[[Any, str], dict | None] | None = None,
) -> list[Any]:
    """Best-effort pivotal excerpts for one session (empty on any failure).

    User corrections first (highest signal), then error->recovery pairs. Injected
    into ``select_skill_candidates`` as its ``excerpt_loader`` so objective feedback
    both boosts the candidate's rank and rides along to the distill prompt.
    Callers must only pass ALREADY-gated session ids (hold-state + excluded-project
    checks happen in selection), so nothing new can leak into that prompt.
    """
    if loader is None:
        from ..workbench.index import get_session_detail
        loader = get_session_detail
    try:
        detail = loader(conn, session_id)
        messages = (detail or {}).get("messages") or []
        corrections = extract_correction_turns(messages)
        env = extract_error_recoveries(messages)
        return (corrections + env)[:max_excerpts]
    except Exception as exc:  # a bad blob must never sink the run
        logger.warning("turn extraction failed for %s: %s", session_id, exc)
        return []


# --- cross-session error-signature candidates (objective support) ------------

MIN_SIGNATURE_SESSIONS = 3   # a signature must hit this many sessions to teach
MAX_ENV_CANDIDATES = 3       # appended on top of the ranked pool


def add_env_candidates(
    conn: Any,
    corpus: Any,
    *,
    min_sessions: int = MIN_SIGNATURE_SESSIONS,
    max_candidates: int = MAX_ENV_CANDIDATES,
    loader: Callable[[Any, str], dict | None] | None = None,
    now: Any = None,
) -> None:
    """Append avoid-candidates for tool-error signatures recurring across sessions.

    This is the judge-free evidence path: ``support_count`` is the number of
    DISTINCT sessions that hit the same normalized tool error — a real count the
    user can verify, not an AI-inferred failure mode. Scans only
    ``corpus.eligible_session_ids`` (already hold-state + exclusion gated), so the
    egress surface is identical to the rest of the corpus. Best-effort per session.
    """
    from .select import SkillCandidate, _candidate_rank, _recency_weight

    if loader is None:
        from ..workbench.index import get_session_detail
        loader = get_session_detail
    hits: dict[str, dict[str, Any]] = {}
    for sid in getattr(corpus, "eligible_session_ids", []) or []:
        try:
            detail = loader(conn, sid) or {}
            messages = detail.get("messages") or []
        except Exception as exc:
            logger.warning("env-signature scan failed for %s: %s", sid, exc)
            continue
        pending_sig: dict[str, str] = {}   # tool -> signature of its latest error
        for m in messages:
            if m.get("role") != "assistant":
                continue
            for tu in extract_tool_uses(m):
                tool = tu.get("tool", "")
                sig = _teachable_error(tu) if tu.get("status") == "error" else ""
                if sig:
                    entry = hits.setdefault(sig, {
                        "sessions": [], "tool": tool,
                        "action": _head(_action_desc(tu), _ACTION_CHARS),
                        "error": _head(_error_text(tu.get("output")), _ERROR_CHARS),
                        "recovery": "",
                    })
                    if sid not in entry["sessions"]:   # count each session ONCE
                        entry["sessions"].append(sid)
                        entry["project"] = detail.get("project") or ""
                        entry["source"] = detail.get("source") or ""
                        entry["start_time"] = detail.get("start_time")
                    pending_sig[tool] = sig
                elif tu.get("status") != "error" and tool in pending_sig:
                    entry = hits.get(pending_sig.pop(tool))
                    if entry is not None and not entry["recovery"]:
                        entry["recovery"] = _head(_action_desc(tu), _ACTION_CHARS)

    clock = now or datetime.now(timezone.utc)
    recurring = sorted(
        (item for item in hits.items() if len(item[1]["sessions"]) >= min_sessions),
        key=lambda item: -len(item[1]["sessions"]),
    )[:max_candidates]
    for idx, (sig, info) in enumerate(recurring):
        n = len(info["sessions"])
        recency = _recency_weight(info.get("start_time"), now=clock)
        excerpt = EnvExcerpt(action=info["action"], error=info["error"],
                             recovery=info["recovery"])
        corpus.failures.append(SkillCandidate(
            # a SYNTHETIC id (not a real session): these clusters summarize many
            # sessions, and reusing a member's real id would collide with that session's
            # own pool candidate in _candidate_aliases — bleeding support across the alias.
            session_id=f"env-signature-{idx}",
            project=info.get("project", ""), source=info.get("source", ""),
            kind="avoid",
            learning_summary=(
                f"Objective environment feedback: the tool error '{sig}' recurred in "
                f"{n} distinct sessions this window (support_count is that session "
                f"count — ground truth, not judge-inferred)."),
            title=f"Recurring {info['tool'] or 'tool'} error",
            support_count=n, impact=2.0, recency=recency,
            rank_score=_candidate_rank(support=n, impact=2.0, recency=recency,
                                       corrections=1),
            pivotal_excerpts=[excerpt],
        ))
        corpus.total_failures += 1


# --- human-rejection candidate (permission denials / reject button) ----------

MIN_REJECTION_SESSIONS = 3

_REJECT_TAG_RE = re.compile(r"reason: \[([^\]]{4,60})\]", re.I)   # [Git Destructive]
_REJECT_FREE_RE = re.compile(r"reason: ([^\n]{4,60})", re.I)


def _rejection_reason(err: str) -> str:
    m = _REJECT_TAG_RE.search(err) or _REJECT_FREE_RE.search(err)
    if m:
        return m.group(1).strip()
    if "doesn't want to take this action" in err.lower():
        return "user pressed reject"
    return "permission denied"


def add_rejection_candidate(
    conn: Any,
    corpus: Any,
    *,
    min_sessions: int = MIN_REJECTION_SESSIONS,
    loader: Callable[[Any, str], dict | None] | None = None,
    now: Any = None,
) -> None:
    """Append ONE avoid-candidate summarizing HUMAN-rejection feedback.

    The reject button, classifier denials, and permission denials are the user
    (or their configured gate) saying no to a specific attempted action — direct
    human feedback the correction heuristic never sees. ``support_count`` is the
    number of distinct sessions containing at least one rejection (a real count).
    Excerpts render as pivotal_turns: agent_before = the attempted action,
    user_correction = the rejection reason. Scans only the gated session ids.
    """
    from .select import SkillCandidate, _candidate_rank, _recency_weight

    if loader is None:
        from ..workbench.index import get_session_detail
        loader = get_session_detail
    sessions: list[str] = []
    reasons: Counter[str] = Counter()
    excerpts: list[TurnExcerpt] = []
    last_detail: dict[str, Any] = {}
    for sid in getattr(corpus, "eligible_session_ids", []) or []:
        try:
            detail = loader(conn, sid) or {}
            messages = detail.get("messages") or []
        except Exception as exc:
            logger.warning("rejection scan failed for %s: %s", sid, exc)
            continue
        found_here = False
        for m in messages:
            if m.get("role") != "assistant":
                continue
            for tu in extract_tool_uses(m):
                if tu.get("status") != "error":
                    continue
                err = _error_text(tu.get("output"))
                if not _HUMAN_REJECTION_RE.search(err):
                    continue
                reason = _rejection_reason(err)
                reasons[reason] += 1
                if not found_here:
                    found_here = True
                    sessions.append(sid)
                    last_detail = detail
                    if len(excerpts) < 3:   # sample from distinct sessions only
                        excerpts.append(TurnExcerpt(
                            before=f"attempted: {_head(_action_desc(tu), _ACTION_CHARS)}",
                            correction=f"rejected: {_head(err, _ERROR_CHARS)}",
                            after="",
                        ))
    n = len(sessions)
    if n < min_sessions:
        return
    clock = now or datetime.now(timezone.utc)
    recency = _recency_weight(last_detail.get("start_time"), now=clock)
    top = ", ".join(f"{r}×{k}" for r, k in reasons.most_common(4))
    corpus.failures.append(SkillCandidate(
        session_id="human-rejection",   # synthetic id: summarizes many sessions (see add_env_candidates)
        project=last_detail.get("project") or "", source=last_detail.get("source") or "",
        kind="avoid",
        learning_summary=(
            f"Human rejection feedback: the user or their permission gate declined "
            f"{sum(reasons.values())} attempted actions across {n} distinct sessions "
            f"(support_count is that session count — ground truth). "
            f"Recurring rejection classes: {top}. Teach the habit of proposing or "
            f"asking first for this class of action instead of attempting it."),
        title="User-Rejected Actions",
        support_count=n, impact=2.0, recency=recency,
        rank_score=_candidate_rank(support=n, impact=2.0, recency=recency,
                                   corrections=len(excerpts)),
        pivotal_excerpts=excerpts,
    ))
    corpus.total_failures += 1
