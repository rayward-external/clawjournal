"""Convert clawjournal session exports to provider-agnostic training format.

Handles:
- User message envelope stripping (Sender metadata, System async echoes, timestamps)
- [[reply_to_current]] protocol marker removal
- Async exec->process(poll) collapsing into single tool_call/tool_result pairs
- Thinking + narration merging into reasoning
- Infrastructure parameter stripping from tool inputs
- Tool output cleaning (TUI box-drawing)

Output: one JSONL line per turn (user message -> agent loop -> reply).
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# User message cleaning
# ---------------------------------------------------------------------------

# Matches the full OpenClaw envelope:
#   [optional System: ... prefix]
#   Sender (untrusted metadata):
#   ```json { ... } ```
#   [timestamp] <actual text>
_SYSTEM_PREFIX_RE = re.compile(
    r"^System:\s*\[.*?\]\s*Exec completed\s*\(.*?\)\s*::.*?(?=Sender \(untrusted metadata\)|$)",
    re.DOTALL,
)
_SENDER_BLOCK_RE = re.compile(
    r"Sender \(untrusted metadata\):\s*```json\s*\{.*?\}\s*```\s*",
    re.DOTALL,
)
_TIMESTAMP_PREFIX_RE = re.compile(
    r"^\[.*?\]\s*",
)


def extract_user_text(content: str) -> str:
    """Strip OpenClaw envelope from user message, return clean text."""
    text = content
    text = _SYSTEM_PREFIX_RE.sub("", text).strip()

    # Only strip timestamp prefix if we actually removed an OpenClaw envelope
    had_envelope = bool(_SENDER_BLOCK_RE.search(text))
    text = _SENDER_BLOCK_RE.sub("", text).strip()
    if had_envelope:
        text = _TIMESTAMP_PREFIX_RE.sub("", text).strip()

    return text


# ---------------------------------------------------------------------------
# Tool input cleaning
# ---------------------------------------------------------------------------

_EXEC_INFRA_KEYS = {"workdir", "yieldMs", "timeout", "elevated", "host", "security", "ask"}
_PROCESS_INFRA_KEYS = {"timeout", "offset", "limit"}


def clean_tool_input(tool_name: str, raw_input: dict) -> dict:
    """Remove infrastructure-only parameters from tool inputs."""
    if tool_name == "exec":
        return {k: v for k, v in raw_input.items() if k not in _EXEC_INFRA_KEYS}
    if tool_name == "process":
        return {k: v for k, v in raw_input.items() if k not in _PROCESS_INFRA_KEYS}
    return raw_input


# ---------------------------------------------------------------------------
# Tool output cleaning
# ---------------------------------------------------------------------------

_BOX_CHARS = set("─│┌┐└┘├┤┬┴┼╭╮╰╯╱╲╳═║╔╗╚╝╠╣╦╩╬◇◆●○■□▪▫▸▹►◂◃◄")
_BOX_LEADER_RE = re.compile(r"^[│┃┆┇┊┋]+\s?")
_BOX_TRAILER_RE = re.compile(r"\s?[│┃┆┇┊┋]+$")


def clean_tool_output(text: str) -> str:
    """Clean TUI box-drawing formatting from tool outputs.

    Strips lines that are purely decorative (>80% box-drawing chars) and
    removes box-drawing leaders/trailers from content lines while
    preserving indentation and blank lines.
    """
    if not text:
        return text
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        # Drop lines that are purely box-drawing decoration
        if stripped and len(stripped) > 3:
            box_count = sum(1 for c in stripped if c in _BOX_CHARS)
            if box_count / len(stripped) > 0.8:
                continue
        # Strip box-drawing leaders/trailers but keep indentation
        content = _BOX_LEADER_RE.sub("", line)
        content = _BOX_TRAILER_RE.sub("", content)
        cleaned.append(content)
    return "\n".join(cleaned)


# ---------------------------------------------------------------------------
# Async exec->process collapsing
# ---------------------------------------------------------------------------

_SESSION_ID_RE = re.compile(r"Command still running \(session (\S+),")


def _is_still_running(output: dict | None) -> str | None:
    """If tool output is 'still running', return the session ID. Else None."""
    if not output:
        return None
    text = output.get("text") or ""
    m = _SESSION_ID_RE.search(text)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Turn grouping
# ---------------------------------------------------------------------------

def group_turns(msgs: list[dict]) -> list[dict]:
    """Group messages into turns: user -> WORK* -> REPLY.

    Each turn has:
      user_msg: the user message dict
      work_msgs: list of intermediate assistant messages (WORK)
      reply_msg: the final assistant message (REPLY), or None
    """
    turns: list[dict] = []
    current: dict | None = None
    for msg in msgs:
        role = msg.get("role")
        if role == "user":
            if current is not None:
                turns.append(current)
            current = {"user_msg": msg, "work_msgs": [], "reply_msg": None}
        elif role == "assistant" and current is not None:
            content = msg.get("content") or ""
            if "[[reply_to_current]]" in content:
                current["reply_msg"] = msg
            else:
                current["work_msgs"].append(msg)
    if current is not None:
        turns.append(current)
    return turns


# ---------------------------------------------------------------------------
# Output building
# ---------------------------------------------------------------------------

def _make_tc_id() -> str:
    return f"tc_{uuid.uuid4().hex[:8]}"


def build_output_sequence(
    work_msgs: list[dict],
    reply_msg: dict | None,
) -> list[dict]:
    """Convert WORK messages + REPLY into a flat output sequence.

    Collapses exec->process(poll) async chains into single tool_call/tool_result.
    Merges thinking + narration into reasoning blocks.
    """
    output: list[dict] = []
    pending_async: dict[str, tuple[str, dict]] = {}

    for msg in work_msgs:
        thinking = msg.get("thinking") or ""
        content = msg.get("content") or ""
        tool_uses = msg.get("tool_uses") or []

        # Merge thinking + narration into reasoning
        reasoning_parts = []
        if thinking:
            reasoning_parts.append(thinking.strip())
        if content:
            reasoning_parts.append(content.strip())
        if reasoning_parts:
            output.append({"type": "reasoning", "text": "\n\n".join(reasoning_parts)})

        for tu in tool_uses:
            tool_name = tu.get("tool", "unknown")
            raw_input = tu.get("input", {})
            raw_output = tu.get("output")
            status = tu.get("status", "unknown")

            # Process(poll/log) resolving a pending async exec
            if tool_name == "process" and raw_input.get("sessionId"):
                session_id = raw_input["sessionId"]
                if session_id in pending_async:
                    tc_id, _ = pending_async.pop(session_id)
                    result_text = ""
                    if raw_output:
                        result_text = raw_output.get("text") or ""
                    if result_text and result_text != f"No session found for {session_id}":
                        output.append({
                            "type": "tool_result", "id": tc_id,
                            "output": clean_tool_output(result_text), "status": "success",
                        })
                    else:
                        output.append({
                            "type": "tool_result", "id": tc_id,
                            "output": None, "status": "lost",
                        })
                    continue
                # Orphaned process call (not matching a pending async) — skip it
                continue

            # Regular tool call
            tc_id = _make_tc_id()
            clean_input = clean_tool_input(tool_name, raw_input)
            output.append({
                "type": "tool_call", "id": tc_id,
                "name": tool_name, "arguments": clean_input,
            })

            async_sid = _is_still_running(raw_output)
            if async_sid:
                pending_async[async_sid] = (tc_id, clean_input)
            else:
                result_text = (raw_output.get("text") or "") if raw_output else ""
                output.append({
                    "type": "tool_result", "id": tc_id,
                    "output": clean_tool_output(result_text), "status": status,
                })

    # Unresolved async execs
    for _session_id, (tc_id, _) in pending_async.items():
        output.append({"type": "tool_result", "id": tc_id, "output": None, "status": "lost"})

    # REPLY message
    if reply_msg:
        thinking = reply_msg.get("thinking") or ""
        content = reply_msg.get("content") or ""
        if thinking:
            output.append({"type": "reasoning", "text": thinking.strip()})
        reply_text = content.replace("[[reply_to_current]]", "").strip()
        if reply_text:
            output.append({"type": "message", "content": reply_text})

    return output


# ---------------------------------------------------------------------------
# Session stats extraction
# ---------------------------------------------------------------------------

def _extract_session_stats(session: dict) -> dict:
    """Extract session-level stats useful for filtering/analysis."""
    return {
        "user_messages": session.get("user_messages"),
        "assistant_messages": session.get("assistant_messages"),
        "tool_uses": session.get("tool_uses"),
        "input_tokens": session.get("input_tokens"),
        "output_tokens": session.get("output_tokens"),
        "duration_seconds": session.get("duration_seconds"),
    }


def _parse_json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, str)]
    return []


def _extract_failure_annotations(session: dict) -> dict:
    """Extract session-level failure labels for each exported training turn."""
    detail = session.get("ai_scoring_detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            detail = {}
    if not isinstance(detail, dict):
        detail = {}

    return {
        "ai_quality_score": session.get("ai_quality_score"),
        "ai_failure_value_score": session.get("ai_failure_value_score"),
        "ai_recovery_labels": _parse_json_list(session.get("ai_recovery_labels")),
        "ai_failure_attribution": session.get("ai_failure_attribution"),
        "ai_failure_modes": _parse_json_list(session.get("ai_failure_modes")),
        "ai_learning_summary": session.get("ai_learning_summary"),
        "ai_failure_evidence": _parse_json_list(detail.get("ai_failure_evidence")),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def convert_session(session: dict) -> list[dict]:
    """Convert one clawjournal session into a list of training turns."""
    msgs = session.get("messages", [])
    turns = group_turns(msgs)
    result = []

    session_id = session.get("session_id", "unknown")
    model = session.get("model", "unknown")
    source = session.get("source", "unknown")
    session_stats = _extract_session_stats(session)
    failure_annotations = _extract_failure_annotations(session)

    for turn_idx, turn in enumerate(turns):
        user_text = extract_user_text(turn["user_msg"].get("content") or "")
        if not user_text:
            continue
        output_seq = build_output_sequence(turn["work_msgs"], turn["reply_msg"])
        if not output_seq:
            continue

        result.append({
            "turn_id": f"{session_id}_{turn_idx:03d}",
            "session_id": session_id,
            "turn_index": turn_idx,
            "model": model,
            "source": source,
            "input": {"role": "user", "content": user_text},
            "output": output_seq,
            "session_stats": session_stats,
            "metadata": {
                "timestamp": turn["user_msg"].get("timestamp"),
                "failure_annotations": failure_annotations,
            },
        })

    return result


def convert_sessions_to_training(
    sessions: Iterable[dict],
    output_path: Path,
) -> dict[str, Any]:
    """Convert sessions JSONL to training-format JSONL.

    Returns summary dict with counts.
    """
    total_turns = 0
    total_sessions = 0
    with open(output_path, "w", encoding="utf-8") as out:
        for session in sessions:
            total_sessions += 1
            turns = convert_session(session)
            for turn in turns:
                out.write(json.dumps(turn, ensure_ascii=False) + "\n")
                total_turns += 1

    return {
        "sessions": total_sessions,
        "turns": total_turns,
        "output": str(output_path),
    }
