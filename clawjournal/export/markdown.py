"""Export sessions as readable Markdown documents."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..pricing import estimate_cost, format_cost


def render_session_markdown(session: dict[str, Any]) -> str:
    """Render a full session as a readable Markdown document.

    Includes every message, tool call, and thinking block.
    Tool outputs and thinking blocks are wrapped in <details> tags.
    """
    parts: list[str] = []

    # Header
    title = (
        session.get("ai_display_title")
        or session.get("display_title")
        or session.get("session_id", "Untitled Session")
    )
    parts.append(f"# {title}\n")

    # Metadata line
    meta_items: list[str] = []
    if session.get("source"):
        meta_items.append(f"**Source:** {session['source'].title()}")
    if session.get("model"):
        meta_items.append(f"**Model:** {session['model']}")
    duration = session.get("duration_seconds")
    if duration:
        meta_items.append(f"**Duration:** {_format_duration(duration)}")
    parts.append(" · ".join(meta_items))

    # Token and cost line
    token_items: list[str] = []
    in_tok = session.get("input_tokens") or 0
    out_tok = session.get("output_tokens") or 0
    if in_tok or out_tok:
        token_items.append(f"**Tokens:** {_format_tokens(in_tok)} in / {_format_tokens(out_tok)} out")
    cost = session.get("estimated_cost_usd") or estimate_cost(
        session.get("model"), in_tok, out_tok
    )
    if cost is not None:
        token_items.append(f"**Cost:** ~{format_cost(cost)}")
    if token_items:
        parts.append(" · ".join(token_items))

    # Date and branch
    date_items: list[str] = []
    if session.get("start_time"):
        date_items.append(f"**Date:** {session['start_time']}")
    if session.get("git_branch"):
        date_items.append(f"**Branch:** {session['git_branch']}")
    if date_items:
        parts.append(" · ".join(date_items))

    parts.append("\n---\n")

    # Messages
    messages = session.get("messages", [])
    turn_number = 0
    last_user = False

    for msg in messages:
        role = msg.get("role", "")

        # Tool use entries (parsed clawjournal format)
        if msg.get("tool"):
            tool_name = msg["tool"]
            tool_input = msg.get("input", {})
            tool_output = msg.get("output", "")
            status = msg.get("status", "")

            # Format tool input
            input_summary = ""
            if isinstance(tool_input, dict):
                if tool_input.get("file_path"):
                    input_summary = f" `{tool_input['file_path']}`"
                elif tool_input.get("command"):
                    input_summary = f" `{tool_input['command'][:100]}`"
                elif tool_input.get("pattern"):
                    input_summary = f" `{tool_input['pattern']}`"

            status_icon = " ✗" if status == "error" else ""
            parts.append(f"**Tool: {tool_name}**{input_summary}{status_icon}")

            if tool_output:
                output_str = str(tool_output)
                lines = output_str.split("\n")
                line_count = len(lines)
                # Truncate long outputs
                if line_count > 50:
                    truncated = "\n".join(lines[:50])
                    parts.append(
                        f"<details><summary>Output ({line_count} lines)</summary>\n\n"
                        f"```\n{truncated}\n[... {line_count - 50} more lines]\n```\n\n</details>\n"
                    )
                else:
                    parts.append(
                        f"<details><summary>Output ({line_count} lines)</summary>\n\n"
                        f"```\n{output_str}\n```\n\n</details>\n"
                    )
            continue

        if role == "user":
            if not last_user:
                turn_number += 1
                parts.append(f"## Turn {turn_number}\n")
            last_user = True

            content = _extract_text(msg)
            if content:
                parts.append(f"**User:**\n{content}\n")

        elif role == "assistant":
            last_user = False

            # Thinking blocks
            thinking = msg.get("thinking", "")
            if thinking:
                parts.append(
                    f"<details><summary>Thinking</summary>\n\n{thinking}\n\n</details>\n"
                )

            # Content
            content = _extract_text(msg)
            if content:
                parts.append(f"**Assistant:**\n{content}\n")

            # Inline tool uses (Anthropic API format)
            content_blocks = msg.get("content")
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_name = block.get("name", "")
                        tool_input = block.get("input", {})
                        input_summary = ""
                        if isinstance(tool_input, dict):
                            if tool_input.get("file_path"):
                                input_summary = f" `{tool_input['file_path']}`"
                            elif tool_input.get("command"):
                                input_summary = f" `{tool_input['command'][:100]}`"
                        parts.append(f"**Tool: {tool_name}**{input_summary}\n")

    # Footer: files and commands
    parts.append("\n---\n")

    files = session.get("files_touched", [])
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except (json.JSONDecodeError, ValueError):
            files = []
    if files:
        parts.append("## Files Touched")
        for f in files[:30]:
            parts.append(f"- `{f}`")
        if len(files) > 30:
            parts.append(f"- *... and {len(files) - 30} more*")
        parts.append("")

    commands = session.get("commands_run", [])
    if isinstance(commands, str):
        try:
            commands = json.loads(commands)
        except (json.JSONDecodeError, ValueError):
            commands = []
    if commands:
        parts.append("## Commands Run")
        for c in commands[:20]:
            parts.append(f"- `{c[:120]}`")
        if len(commands) > 20:
            parts.append(f"- *... and {len(commands) - 20} more*")
        parts.append("")

    return "\n".join(parts)


def render_session_summary(session: dict[str, Any]) -> str:
    """Render a concise AI-generated summary of a session.

    Uses the AI scoring fields (summary, tags, resolution) when available.
    Falls back to basic metadata if the session hasn't been scored.
    """
    parts: list[str] = []

    title = (
        session.get("ai_display_title")
        or session.get("display_title")
        or session.get("session_id", "Untitled Session")
    )
    parts.append(f"# {title}\n")

    # Metadata
    meta_items: list[str] = []
    if session.get("source"):
        meta_items.append(f"**Source:** {session['source'].title()}")
    if session.get("model"):
        meta_items.append(f"**Model:** {session['model']}")
    duration = session.get("duration_seconds")
    if duration:
        meta_items.append(f"**Duration:** {_format_duration(duration)}")
    if meta_items:
        parts.append(" · ".join(meta_items))

    # Score and resolution
    score = session.get("ai_quality_score")
    if score is not None:
        labels = {1: "Noise", 2: "Minimal", 3: "Light", 4: "Solid", 5: "Major"}
        label = labels.get(score, str(score))
        parts.append(f"**Score:** {score}/5 ({label}) · **Resolution:** {session.get('ai_outcome_badge', 'unknown')}")

    # Tags
    tags = session.get("ai_value_badges")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (json.JSONDecodeError, ValueError):
            tags = []
    task_type = session.get("ai_task_type") or session.get("task_type")
    tag_parts = []
    if task_type:
        tag_parts.append(task_type)
    if isinstance(tags, list):
        tag_parts.extend(tags[:5])
    if tag_parts:
        parts.append(f"**Tags:** {', '.join(tag_parts)}")

    parts.append("")

    # Summary from AI scoring
    summary = session.get("ai_summary")
    if summary:
        parts.append(f"## Summary\n\n{summary}\n")
    else:
        # Fallback: generate basic summary from messages
        messages = session.get("messages", [])
        if messages:
            first_user = next(
                (_extract_text(m) for m in messages if m.get("role") == "user"), None
            )
            if first_user:
                # Truncate to first 200 chars
                preview = first_user[:200].strip()
                if len(first_user) > 200:
                    preview += "..."
                parts.append(f"## Task\n\n{preview}\n")

    # Key stats
    files = session.get("files_touched", [])
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except (json.JSONDecodeError, ValueError):
            files = []
    if files:
        parts.append("## Files Changed")
        for f in files[:10]:
            parts.append(f"- `{f}`")
        if len(files) > 10:
            parts.append(f"- *... and {len(files) - 10} more*")
        parts.append("")

    # Failure analysis (AI judge output — modes, attribution, recovery, evidence)
    failure_block = _render_failure_analysis(session)
    if failure_block:
        parts.append(failure_block)

    # Outcome
    resolution = session.get("ai_outcome_badge") or session.get("outcome_badge")
    if resolution:
        parts.append(f"## Outcome\n\n{resolution.replace('_', ' ').title()}\n")

    return "\n".join(parts)


def _parse_list_field(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value if x]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return []
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x]
    return []


def _render_failure_analysis(session: dict[str, Any]) -> str:
    """Render the Failure Analysis section, or empty string if no failure data.

    Pulls from columns (`ai_failure_modes`, `ai_failure_attribution`,
    `ai_recovery_labels`, `ai_learning_summary`, `ai_failure_value_score`)
    and from the `ai_scoring_detail` JSON blob (`ai_failure_evidence`,
    `ai_meta_labels`). Keeps the AI judge output queryable from exported
    bundles, not just from the live SQLite index.
    """
    detail = _parse_detail_field(session.get("ai_scoring_detail"))

    score = session.get("ai_failure_value_score")
    attribution = str(session.get("ai_failure_attribution") or "").strip()
    modes = _parse_list_field(session.get("ai_failure_modes"))
    recovery = _parse_list_field(session.get("ai_recovery_labels"))
    meta_labels = _parse_list_field(detail.get("ai_meta_labels"))
    evidence = _parse_list_field(detail.get("ai_failure_evidence"))
    learning = str(session.get("ai_learning_summary") or "").strip()

    if not any([score is not None, attribution, modes, recovery, meta_labels, evidence, learning]):
        return ""

    rows: list[str] = ["## Failure Analysis", ""]
    if score is not None:
        rows.append(f"- **Failure value:** {score}/5")
    if attribution:
        rows.append(f"- **Attribution:** {attribution}")
    if modes:
        rows.append(f"- **Modes:** {', '.join(modes)}")
    if recovery:
        rows.append(f"- **Recovery:** {', '.join(recovery)}")
    if meta_labels:
        rows.append(f"- **Meta labels:** {', '.join(meta_labels)}")
    if learning:
        rows.extend(["", f"_{learning}_"])
    if evidence:
        rows.extend(["", "**Evidence:**", ""])
        rows.extend(f"- {e}" for e in evidence)
    rows.append("")
    return "\n".join(rows)


def _parse_detail_field(raw_detail: Any) -> dict[str, Any]:
    if isinstance(raw_detail, dict):
        return raw_detail
    if isinstance(raw_detail, str) and raw_detail.strip():
        try:
            parsed = json.loads(raw_detail)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _extract_text(msg: dict[str, Any]) -> str:
    """Extract plain text from a message's content field."""
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, str):
                text_parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if text:
                    text_parts.append(text)
        return "\n\n".join(text_parts).strip()
    return ""


def _format_duration(seconds: int) -> str:
    """Format seconds into human-readable duration like '8m 12s'."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins}m"


def _format_tokens(count: int) -> str:
    """Format token count like '45K' or '1.2M'."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}K"
    return str(count)
