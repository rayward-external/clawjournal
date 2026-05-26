"""Share card generation for quick trace sharing.

Generates compact, redacted, formatted summaries of sessions
designed for messaging channels (Telegram, Discord, etc.).
"""

import json
from typing import Any

from ..scoring.depth import format_session_at_depth

# Outcome badge → display emoji + label
_OUTCOME_DISPLAY: dict[str, str] = {
    "tests_passed": "\u2705 Tests passed",
    "tests_failed": "\u274c Tests failed",
    "build_failed": "\u274c Build failed",
    "analysis_only": "\ud83d\udd0d Analysis only",
    "completed": "\u2705 Completed",
    "errored": "\u274c Errored",
    "partial": "\u26a0\ufe0f Partial",
    "unknown": "\u2753 Unknown",
    # Legacy/future values
    "success": "\u2705 Success",
    "failed": "\u274c Failed",
}

# Max card_text length — leaves room for MarkdownV2 escaping overhead
MAX_CARD_CHARS = 3500


def _format_duration(seconds: int | None) -> str:
    """Format duration in seconds to human-readable string."""
    if not seconds or seconds <= 0:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    remaining = minutes % 60
    if remaining:
        return f"{hours}h {remaining}m"
    return f"{hours}h"


def _format_tokens(count: int) -> str:
    """Format token count to human-readable string."""
    if count < 1000:
        return str(count)
    if count < 10000:
        return f"{count / 1000:.1f}k"
    return f"{count // 1000}k"


def generate_card(
    session: dict[str, Any],
    depth: str = "summary",
) -> dict[str, Any]:
    """Generate a share card for a session.

    Args:
        session: Full session dict (from get_session_detail).
        depth: One of 'workflow', 'summary', 'full'.

    Returns:
        Dict with: session_id, depth, card (structured), card_text (pre-formatted),
                   redaction_count, next_steps
    """
    formatted = format_session_at_depth(session, depth)

    # Build card structure
    card: dict[str, Any] = {
        "title": formatted["title"],
        "source": session.get("source", ""),
        "model": _short_model_name(session.get("model") or ""),
        "duration_seconds": session.get("duration_seconds"),
        "score": session.get("ai_quality_score"),
        "outcome": session.get("outcome_badge", ""),
        "summary_line": formatted["summary_line"],
        "workflow_steps": formatted["workflow_steps"],
        "workflow_oneliner": formatted["workflow_oneliner"],
        "stats": formatted["stats"],
        "redaction_count": session.get("_redaction_count", 0),
        "failure": _summarize_failure(session),
    }

    # Build pre-formatted card text
    card_text = _build_card_text(card, depth)

    # Truncate if needed
    if len(card_text) > MAX_CARD_CHARS:
        card_text = _truncate_card_text(card, depth)

    return {
        "session_id": session.get("session_id", ""),
        "depth": depth,
        "card": card,
        "card_text": card_text,
        "next_steps": ["Send card_text via messaging channel"],
    }


def _summarize_failure(session: dict[str, Any]) -> dict[str, Any]:
    """Compact failure summary for the share card.

    Cards are size-bounded so we surface only the most analytically
    important fields: score, attribution, and the mode list. The full
    evidence + learning summary live in the trace note and the markdown
    export.
    """
    modes_raw = session.get("ai_failure_modes")
    if isinstance(modes_raw, str):
        try:
            modes_raw = json.loads(modes_raw)
        except (json.JSONDecodeError, ValueError):
            modes_raw = []
    modes = [str(m).replace("_", " ") for m in modes_raw or [] if m]
    return {
        "score": session.get("ai_failure_value_score"),
        "attribution": session.get("ai_failure_attribution") or None,
        "modes": modes,
    }


def _short_model_name(model: str) -> str:
    """Shorten model names for display."""
    if not model:
        return ""
    # claude-sonnet-4-20250514 → sonnet-4
    # claude-opus-4-20250514 → opus-4
    for prefix in ("claude-", "anthropic/", "openai/", "google/"):
        if model.startswith(prefix):
            model = model[len(prefix):]
    # Remove date suffix
    parts = model.rsplit("-", 1)
    if len(parts) == 2 and len(parts[1]) == 8 and parts[1].isdigit():
        model = parts[0]
    return model


def _build_card_text(card: dict[str, Any], depth: str) -> str:
    """Build the pre-formatted plain text version of a card."""
    lines: list[str] = []

    # Title
    title = card["title"] or "Session"
    lines.append(title)

    # Metadata line
    meta_parts: list[str] = []
    if card["source"]:
        meta_parts.append(card["source"].capitalize())
    if card["model"]:
        meta_parts.append(card["model"])
    duration = _format_duration(card.get("duration_seconds"))
    if duration:
        meta_parts.append(duration)
    if meta_parts:
        lines.append(" \u00b7 ".join(meta_parts))

    # Score + outcome line
    badge_parts: list[str] = []
    outcome = card.get("outcome", "")
    if outcome:
        badge_parts.append(_OUTCOME_DISPLAY.get(outcome, outcome))
    score = card.get("score")
    if score:
        badge_parts.append(f"\u2b50 {score}/5")
    if badge_parts:
        lines.append(" \u00b7 ".join(badge_parts))

    # Failure summary line (compact: score \u00b7 attribution \u00b7 modes)
    failure = card.get("failure") or {}
    failure_parts: list[str] = []
    if failure.get("score") is not None:
        failure_parts.append(f"Failure {failure['score']}/5")
    if failure.get("attribution"):
        failure_parts.append(str(failure["attribution"]).replace("_", " "))
    if failure.get("modes"):
        failure_parts.append(", ".join(failure["modes"]))
    if failure_parts:
        lines.append(" \u00b7 ".join(failure_parts))

    lines.append("")  # blank line

    # Summary line
    if card["summary_line"]:
        lines.append(card["summary_line"])
        lines.append("")

    # Workflow
    oneliner = card["workflow_oneliner"]
    if oneliner:
        lines.append(oneliner)
        lines.append("")

    # Stats line
    stats = card["stats"]
    stat_parts: list[str] = []
    total_msgs = stats.get("user_messages", 0) + stats.get("assistant_messages", 0)
    if total_msgs:
        stat_parts.append(f"{total_msgs} msgs")
    tool_count = stats.get("tool_uses", 0)
    if tool_count:
        stat_parts.append(f"{tool_count} tools")
    total_tokens = stats.get("total_tokens", 0)
    if total_tokens:
        stat_parts.append(f"{_format_tokens(total_tokens)} tokens")
    if stat_parts:
        lines.append(" \u00b7 ".join(stat_parts))

    # Redaction footer
    redact_count = card.get("redaction_count", 0)
    if redact_count and depth != "workflow":
        lines.append(f"{redact_count} secrets redacted")

    return "\n".join(lines)


def _truncate_card_text(card: dict[str, Any], depth: str) -> str:
    """Build a truncated version of card text when it exceeds MAX_CARD_CHARS.

    Shortens the workflow section, then title/summary if still over limit.
    """
    card = dict(card)  # shallow copy to avoid mutating the original

    # Step 1: truncate workflow steps
    steps = card.get("workflow_steps", [])
    if len(steps) > 7:
        from ..scoring.depth import format_step_text
        texts = [format_step_text(s) for s in steps[:5]]
        texts.append(f"... {len(steps) - 7} more ...")
        texts.extend(format_step_text(s) for s in steps[-2:])
        card["workflow_oneliner"] = " \u2192 ".join(texts)

    text = _build_card_text(card, depth)

    # Step 2: if still over, truncate summary line
    if len(text) > MAX_CARD_CHARS and card.get("summary_line"):
        card["summary_line"] = card["summary_line"][:80] + "..."
        text = _build_card_text(card, depth)

    # Step 3: if still over, truncate title
    if len(text) > MAX_CARD_CHARS and card.get("title"):
        card["title"] = card["title"][:60] + "..."
        text = _build_card_text(card, depth)

    # Step 4: hard truncate as last resort
    if len(text) > MAX_CARD_CHARS:
        text = text[:MAX_CARD_CHARS - 3] + "..."

    return text
